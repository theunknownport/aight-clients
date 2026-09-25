"""OTel setup + attribution of LLM-cost spans to the user-code line
that issued them, supporting custom labels and explicit trace IDs 
for agnostic business event matching.
"""
from __future__ import annotations

import contextvars
import importlib
import inspect
import json
import linecache
import os
from collections import defaultdict
from dataclasses import dataclass, field
import uuid

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanProcessor

_PROVIDER = TracerProvider()
# Best-effort: makes aight's spans visible to any exporter the host app
# configures. Harmless when the app already set its own provider — OTel
# refuses to override one and only logs a warning.
trace.set_tracer_provider(_PROVIDER)
# Deliberately _PROVIDER.get_tracer, NOT the module-level trace.get_tracer:
# that one reads the *global* provider, so an app that configured its own
# first (opentelemetry-instrument, any APM agent) would send our spans
# somewhere COST_PROCESSOR never sees them — every traced call would record
# nothing, and push() would silently no-op on an empty bucket dict.
_TRACER = _PROVIDER.get_tracer("aight.spike")

# Context variables for agnostic tracking and explicit matching (thread/async safe)
_active_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("_active_trace_id", default=None)
_active_labels: contextvars.ContextVar[dict] = contextvars.ContextVar("_active_labels", default={})


class TraceContext:
    """Context manager to scope custom labels and an explicit trace_id 
    for a block of code or an agent run.
    """
    def __init__(self, trace_id: str | None = None, labels: dict | None = None):
        self.trace_id = trace_id or str(uuid.uuid4())
        self.labels = labels or {}
        self._token_trace = None
        self._token_labels = None

    def __enter__(self):
        self._token_trace = _active_trace_id.set(self.trace_id)
        current = _active_labels.get()
        merged = {**current, **self.labels}
        self._token_labels = _active_labels.set(merged)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._token_trace:
            _active_trace_id.reset(self._token_trace)
        if self._token_labels:
            _active_labels.reset(self._token_labels)


def current_trace_id() -> str | None:
    """The active TraceContext's trace_id, or None outside any context.
    Lets aight.remote.push_event() default a business event's trace_id to
    match whatever agent run it was emitted during, without reaching into
    the private contextvar directly."""
    return _active_trace_id.get()


# How many source lines to grab on each side of an attributed line, for the
# Line Review code panel. Not configurable — a fixed small window is plenty
# for "what's on this line" context and keeps span attributes/payloads small.
_SNIPPET_CONTEXT = 3

# (filepath, lineno) -> (first_lineno_in_snippet, joined source text).
# Populated once per unique line ever seen (linecache itself caches file
# reads, so repeat calls for the same file are cheap too) and read by
# aight.remote.push() to attach source context to each chain frame.
SNIPPETS: dict[tuple[str, int], tuple[int, str]] = {}


def _capture_snippet(filepath: str, lineno: int) -> None:
    """Best-effort: if the file isn't readable (deployed without source,
    path from a different machine, stdin frame, ...) just skip it — a
    missing snippet degrades to the existing 'not captured' UI state,
    it's never worth raising over."""
    if (filepath, lineno) in SNIPPETS:
        return
    linecache.checkcache(filepath)
    start = max(1, lineno - _SNIPPET_CONTEXT)
    lines = linecache.getlines(filepath)[start - 1 : lineno + _SNIPPET_CONTEXT]
    if lines:
        SNIPPETS[(filepath, lineno)] = (start, "".join(lines).rstrip("\n"))


@dataclass
class _Bucket:
    # No `model` field: the model is half of the bucket key (see BucketKey), so
    # every bucket already belongs to exactly one. Keeping a copy here invited
    # the two to disagree.
    calls: int = field(default=0)
    input_tokens: int = field(default=0)
    output_tokens: int = field(default=0)
    # Kept beside input_tokens, never folded into it. The platform sums four
    # rates and freezes the result at receive, so an input count that already
    # included the cached tokens would bill them twice — once at the full input
    # rate and again at the cache rate — and the overcount could never be
    # corrected. See `_usage` for where the two conventions meet.
    cache_read_tokens: int = field(default=0)
    cache_creation_tokens: int = field(default=0)
    # Summed across the bucket's calls; the server divides by calls. Only ever
    # what the caller measured and passed — see traced_llm_call.
    latency_ms: float = field(default=0.0)
    trace_id: str = field(default="")


# One attributed call site, outer-to-leaf.
Chain = tuple[tuple[str, int, str], ...]
# (call chain, model) -> totals. The model belongs in the key, not just on the
# bucket: a single line that calls two models must stay two buckets, each
# priced on its own tokens. Summing them under one model name is what made the
# server reprice the whole aggregate at whichever rate was recorded last.
BucketKey = tuple[Chain, str]


# GenAI semantic conventions have used both key names across versions; check
# both rather than assuming one. Confirm which your installed
# opentelemetry-instrumentation-openai/-anthropic version actually emits
# before relying on this in production.
#
# The third element is a convention, not a name, and the two disagree about
# whether the cached tokens are already inside the input count:
#
#   ...input_tokens   Anthropic-shaped — the uncached prompt only, with the
#                     cache counts reported separately alongside it.
#   ...prompt_tokens  OpenAI-shaped — the *total* prompt, cached tokens
#                     included.
#
# The wire wants the exclusive one (docs/hub/ingestion.md), so an inclusive
# match subtracts the cache read back out below. Skipping that bills a cached
# token twice — once at the full input rate, again at the cache rate — and
# because the platform freezes cost at receive the overcount is permanent.
_TOKEN_ATTR_CANDIDATES = (
    ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", False),
    ("gen_ai.usage.prompt_tokens", "gen_ai.usage.completion_tokens", True),
)

# Where the cache counts live, when the instrumentation reports them at all.
# Both spellings are Anthropic's and the semconv has not settled on one; a
# missing key means "no caching reported", which is also the normal case for a
# provider that has none or a call that did not use it.
_CACHE_READ_ATTRS = ("gen_ai.usage.cache_read_input_tokens", "gen_ai.usage.cached_tokens")
_CACHE_CREATION_ATTRS = ("gen_ai.usage.cache_creation_input_tokens",)


def _first_int(attrs: dict, keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in attrs:
            return int(attrs[key] or 0)
    return 0


def _usage(attrs: dict) -> tuple[int, int, int, int] | None:
    """(input, output, cache_read, cache_creation) as the wire wants them, or
    None when the span reported no usage at all.

    `input` is always the *uncached* prompt by the time this returns. That is
    the one guarantee the function exists to make."""
    for input_key, output_key, inclusive in _TOKEN_ATTR_CANDIDATES:
        if input_key not in attrs or output_key not in attrs:
            continue
        cache_read = _first_int(attrs, _CACHE_READ_ATTRS)
        cache_creation = _first_int(attrs, _CACHE_CREATION_ATTRS)
        prompt = int(attrs[input_key] or 0)
        # Only an inclusive count can contain a cache figure, and only the read
        # one: a cache *write* is tokens being processed for the first time,
        # not a repeat of ones already in the prompt. Clamped at zero because
        # instrumentation that reports the cache count larger than the prompt
        # it is supposedly inside is telling us its own numbers disagree, and a
        # negative token count would be worse than a floor.
        uncached = max(prompt - cache_read, 0) if inclusive else prompt
        return uncached, int(attrs[output_key] or 0), cache_read, cache_creation
    return None


class CostByLineProcessor(SpanProcessor):
    """Aggregates LLM usage by the full call chain (a tuple of (file, line,
    function) frames, outer-to-leaf) that led to the call, not just the
    immediate caller. Deliberately records no cost: pricing belongs to the
    platform, which reprices every row from its model and token counts."""

    def __init__(self) -> None:
        self.buckets: dict[BucketKey, _Bucket] = defaultdict(_Bucket)

    def on_start(self, span, parent_context=None) -> None:
        if not span.attributes or "code.chain.length" not in span.attributes:
            chain = _resolve_caller_chain()
            _set_chain_attributes(span, chain)

    def on_end(self, span) -> None:
        attrs = span.attributes or {}
        model = attrs.get("gen_ai.request.model") or ""
        usage = _usage(attrs)
        # This processor sits on the global provider, so it sees *every* span in
        # the host process. Without this guard each unrelated span becomes a
        # calls=1 row with no model and no tokens.
        if not model or usage is None:
            return
        bucket = self.buckets[(_chain_from_attrs(attrs), model)]
        bucket.calls += 1
        bucket.input_tokens += usage[0]
        bucket.output_tokens += usage[1]
        bucket.cache_read_tokens += usage[2]
        bucket.cache_creation_tokens += usage[3]
        # Only what the caller measured and handed in. This used to add the
        # span's own duration, which is not the LLM call: traced_llm_call runs
        # *after* the call returns, so what it timed was the cost of recording
        # the numbers — around 2ms for a call that actually took two seconds.
        # A figure that wrong is worse than none, so an untimed call adds
        # nothing here and the Workspace shows its "—" instead.
        latency = attrs.get("aight.latency_ms")
        if latency:
            bucket.latency_ms += float(latency)
        trace_id = attrs.get("aight.trace_id")
        if trace_id:
            bucket.trace_id = trace_id

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def report(self) -> str:
        """Calls and tokens per attributed line — no dollar figure, because the
        SDK has no price table. The Workspace reprices these rows and is where
        the spend lives."""
        lines = []
        for (chain, model), b in sorted(self.buckets.items(), key=lambda kv: -kv[1].calls):
            leaf_path, leaf_lineno, leaf_fn = chain[-1]
            where = f"{os.path.basename(leaf_path)}:{leaf_lineno} ({leaf_fn})"
            if model:
                where += f" [{model}]"
            lines.append(f"{where} = {b.calls} call(s), {b.input_tokens} in / {b.output_tokens} out")
        return "\n".join(lines)


COST_PROCESSOR = CostByLineProcessor()
_PROVIDER.add_span_processor(COST_PROCESSOR)


def _framework_dirs() -> tuple[str, ...]:
    """Installed framework, OTel SDK, and auto-instrumentation package
    directories whose frames sit between user code and the traced call and
    must be skipped."""
    dirs = []
    for modname in (
        "langgraph",
        "langchain_core",
        "langchain",
        "opentelemetry.sdk.trace",
        "opentelemetry.instrumentation.openai",
        "opentelemetry.instrumentation.anthropic",
        "openai",
        "anthropic",
    ):
        try:
            mod = importlib.import_module(modname)
            if mod.__file__:
                dirs.append(os.path.dirname(os.path.abspath(mod.__file__)))
        except (ImportError, AttributeError):
            pass
    try:
        mod = importlib.import_module("opentelemetry")
        if mod.__path__:
            dirs.append(mod.__path__[0])
    except (ImportError, AttributeError):
        pass
    return tuple(dirs)


def _test_harness_dirs() -> tuple[str, ...]:
    """pytest/pluggy package directories."""
    dirs = []
    for modname in ("pytest", "_pytest", "pluggy"):
        try:
            mod = importlib.import_module(modname)
            if mod.__file__:
                dirs.append(os.path.dirname(os.path.abspath(mod.__file__)))
        except (ImportError, AttributeError):
            pass
    return tuple(dirs)


_THIS_FILE = os.path.abspath(__file__)
_SKIP_DIRS = _framework_dirs()
_CHAIN_SKIP_DIRS = _SKIP_DIRS + _test_harness_dirs()
_TESTS_DIR = os.path.join(os.path.dirname(os.path.dirname(_THIS_FILE)), "tests") + os.sep


def _resolve_caller_frame() -> tuple[str, int, str]:
    """Walk the stack past this exact module and known framework packages to
    find the first frame belonging to user code."""
    stack = inspect.stack()[1:]
    for frame_info in stack:
        path = os.path.abspath(frame_info.filename)
        if path == _THIS_FILE:
            continue
        if path.startswith(_SKIP_DIRS):
            continue
        return path, frame_info.lineno, frame_info.function
    last = stack[-1]
    return os.path.abspath(last.filename), last.lineno, last.function


def _resolve_caller_chain(max_depth: int = 5) -> tuple[tuple[str, int, str], ...]:
    """Like _resolve_caller_frame, but collects every consecutive
    user-code frame instead of stopping at the first one."""
    stack = inspect.stack()[1:]
    leaf_to_outer: list[tuple[str, int, str]] = []
    test_frame: tuple[str, int, str] | None = None
    for frame_info in stack:
        path = os.path.abspath(frame_info.filename)
        if path == _THIS_FILE or path.startswith(_CHAIN_SKIP_DIRS):
            continue
        if path.startswith(_TESTS_DIR) and frame_info.function.startswith("test_"):
            test_frame = (path, frame_info.lineno, frame_info.function)
            break
        leaf_to_outer.append((path, frame_info.lineno, frame_info.function))
        if len(leaf_to_outer) >= max_depth:
            break
    if not leaf_to_outer:
        if test_frame:
            leaf_to_outer = [test_frame]
        else:
            last = stack[-1]
            leaf_to_outer = [(os.path.abspath(last.filename), last.lineno, last.function)]
    for path, lineno, _function in leaf_to_outer:
        _capture_snippet(path, lineno)
    return tuple(reversed(leaf_to_outer))


def _set_chain_attributes(span, chain: tuple[tuple[str, int, str], ...]) -> None:
    span.set_attribute("code.chain.length", len(chain))
    for i, (filepath, lineno, function) in enumerate(chain):
        span.set_attribute(f"code.chain.{i}.filepath", filepath)
        span.set_attribute(f"code.chain.{i}.lineno", lineno)
        span.set_attribute(f"code.chain.{i}.function", function)


def _chain_from_attrs(attrs: dict) -> tuple[tuple[str, int, str], ...]:
    length = attrs.get("code.chain.length")
    if not length:
        return ((attrs.get("code.filepath", "?"), attrs.get("code.lineno", 0), attrs.get("code.function", "?")),)
    return tuple(
        (
            attrs.get(f"code.chain.{i}.filepath", "?"),
            attrs.get(f"code.chain.{i}.lineno", 0),
            attrs.get(f"code.chain.{i}.function", "?"),
        )
        for i in range(length)
    )


VALUE_BY_FILE: dict[str, float] = defaultdict(float)


def report_value(value_usd: float) -> None:
    """Record revenue/value this agent earned, attributed to the same "agent"
    identity traced_llm_call uses for spend: the outermost frame of the call
    chain, which is the file the fleet view groups agents by.

    Deliberately the outermost frame, not the immediate caller — resolving the
    caller here (as this did) splits one agent in two whenever report_value is
    called from a helper: spend attributed to the outer file, earnings to the
    helper, and a spent-vs-earned view that never lines up."""
    filepath, _lineno, _function = _resolve_caller_chain()[0]
    VALUE_BY_FILE[filepath] += value_usd


def traced_llm_call(
    model: str,
    input_tokens: int,
    output_tokens: int,
    trace_id: str | None = None,
    labels: dict | None = None,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    latency_ms: float | None = None,
) -> None:
    """Record an LLM call as an OTel span, attributed to the full user-code call
    chain, with optional explicit trace_id and custom labels, so the platform can
    price it. Returns None: there is no price table here by design — the model
    and the four token counts are sent, and the AIght Workspace is where the
    dollar figure lives.

    The cache counts are keyword-only and default to zero so the signature stays
    backwards compatible, but passing them is not optional in spirit: a
    cache-heavy call sent without them is priced as if nothing were cached, and
    `input_tokens` must be the *uncached* prompt — the Anthropic convention,
    which every provider's numbers convert to before they reach here. See
    docs/hub/ingestion.md.

    `latency_ms` is yours to measure. This SDK does not time the LLM call: it is
    called *after* your call returns, not around it, so there is nothing here to
    time — timing the span would measure the cost of recording, not the call.
    Time the call yourself and pass the milliseconds; leave it out and no
    latency is claimed, which the Workspace shows as "—" on the Speed axis
    rather than as an impossibly fast number. Zero or negative is read the same
    way by the platform, so there is no reason to send one.
    """
    chain = _resolve_caller_chain()

    # Resolve active trace ID and labels from parameters or active TraceContext
    active_trace_id = trace_id or _active_trace_id.get()
    active_labels = {**_active_labels.get(), **(labels or {})}

    with _TRACER.start_as_current_span("llm.call") as span:
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        # Set even at zero: a span that names all four fields reads the same
        # whether or not the call used caching, and `_usage` treats presence as
        # "reported", which is exactly what this is.
        span.set_attribute("gen_ai.usage.cache_read_input_tokens", cache_read_tokens)
        span.set_attribute("gen_ai.usage.cache_creation_input_tokens", cache_creation_tokens)
        # Set only when the caller actually measured it. An absent attribute is
        # what on_end reads as "nobody timed this call".
        if latency_ms is not None:
            span.set_attribute("aight.latency_ms", latency_ms)
        _set_chain_attributes(span, chain)

        # Attach agnostic business context attributes if present
        if active_trace_id:
            span.set_attribute("aight.trace_id", active_trace_id)
        if active_labels:
            span.set_attribute("aight.labels", json.dumps(active_labels))
