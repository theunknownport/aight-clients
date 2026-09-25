"""Tests for CostByLineProcessor's auto-instrumentation support: spans
created the way an OTel auto-instrumentor creates them (GenAI usage
attributes, no code.*) still get attributed with their model and tokens.
"""
from aight.tracing import COST_PROCESSOR, _TRACER


def test_on_start_backfills_code_location_for_auto_instrumented_spans():
    COST_PROCESSOR.buckets.clear()
    with _TRACER.start_as_current_span("openai.chat") as span:
        span.set_attribute("gen_ai.request.model", "gpt-4o-mini")
        span.set_attribute("gen_ai.usage.input_tokens", 100)
        span.set_attribute("gen_ai.usage.output_tokens", 50)

    assert len(COST_PROCESSOR.buckets) == 1
    (chain, _model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    filepath, lineno, function = chain[-1]  # Get the leaf of the chain
    assert filepath.endswith("test_auto_instrument.py")
    assert function == "test_on_start_backfills_code_location_for_auto_instrumented_spans"
    assert lineno > 0
    assert bucket.calls == 1


def test_on_end_records_gen_ai_usage_attributes():
    COST_PROCESSOR.buckets.clear()
    with _TRACER.start_as_current_span("openai.chat") as span:
        span.set_attribute("gen_ai.request.model", "gpt-4o-mini")
        span.set_attribute("gen_ai.usage.input_tokens", 1000)
        span.set_attribute("gen_ai.usage.output_tokens", 1000)

    (_, model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    assert model == "gpt-4o-mini"
    assert bucket.calls == 1
    assert bucket.input_tokens == 1000
    assert bucket.output_tokens == 1000


def test_a_span_with_neither_model_nor_usage_is_skipped():
    """This processor is registered on the global provider, so it sees every
    span in the host process — an HTTP client's, a DB driver's, another APM
    agent's. Without the guard each one becomes a calls=1 row with no model and
    no tokens, which the platform can only read as a mystery call site."""
    COST_PROCESSOR.buckets.clear()
    with _TRACER.start_as_current_span("some.unrelated.span"):
        pass

    assert COST_PROCESSOR.buckets == {}


def test_an_anthropic_shaped_span_carries_its_cache_counts_alongside():
    """`gen_ai.usage.input_tokens` is the uncached prompt by definition, so the
    cache counts ride beside it and nothing is subtracted."""
    COST_PROCESSOR.buckets.clear()
    with _TRACER.start_as_current_span("anthropic.messages") as span:
        span.set_attribute("gen_ai.request.model", "claude-3-5-sonnet-20241022")
        span.set_attribute("gen_ai.usage.input_tokens", 8241)
        span.set_attribute("gen_ai.usage.output_tokens", 400)
        span.set_attribute("gen_ai.usage.cache_read_input_tokens", 117760)
        span.set_attribute("gen_ai.usage.cache_creation_input_tokens", 3000)

    (_, model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    assert model == "claude-3-5-sonnet-20241022"
    assert bucket.input_tokens == 8241
    assert bucket.output_tokens == 400
    assert bucket.cache_read_tokens == 117760
    assert bucket.cache_creation_tokens == 3000


def test_an_openai_shaped_span_has_the_cached_tokens_subtracted_back_out():
    """The one conversion that has to happen, and the reason it does.

    `gen_ai.usage.prompt_tokens` is the *total* prompt with the cached tokens
    already inside it. Sending that as `input_tokens` while also sending the
    cache count bills those tokens twice — once at the full input rate and again
    at the cache rate — and because the platform freezes cost at receive, the
    overcount is permanent. For gpt-4o that is 3x the true cost on a fully
    cached prompt."""
    COST_PROCESSOR.buckets.clear()
    with _TRACER.start_as_current_span("openai.chat") as span:
        span.set_attribute("gen_ai.request.model", "gpt-4o")
        span.set_attribute("gen_ai.usage.prompt_tokens", 10000)  # inclusive
        span.set_attribute("gen_ai.usage.completion_tokens", 500)
        span.set_attribute("gen_ai.usage.cached_tokens", 9600)

    (_, model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    assert model == "gpt-4o"
    assert bucket.input_tokens == 400  # 10000 - 9600
    assert bucket.cache_read_tokens == 9600
    # The two counts still add up to what the provider actually reported.
    assert bucket.input_tokens + bucket.cache_read_tokens == 10000


def test_a_span_without_cache_attributes_reports_no_cache_tokens():
    COST_PROCESSOR.buckets.clear()
    with _TRACER.start_as_current_span("openai.chat") as span:
        span.set_attribute("gen_ai.request.model", "gpt-4o-mini")
        span.set_attribute("gen_ai.usage.input_tokens", 100)
        span.set_attribute("gen_ai.usage.output_tokens", 50)

    (_, _model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    assert bucket.cache_read_tokens == 0
    assert bucket.cache_creation_tokens == 0


def test_a_cache_only_span_still_becomes_a_row():
    """A call whose prompt is entirely a cache hit reports zeros for the plain
    input and output counts. Dropping it would lose the call outright; the
    cache counts are the whole story and the platform prices them at the
    cache rate, which is about a tenth of the input rate."""
    COST_PROCESSOR.buckets.clear()
    with _TRACER.start_as_current_span("anthropic.messages") as span:
        span.set_attribute("gen_ai.request.model", "claude-3-opus-20240229")
        span.set_attribute("gen_ai.usage.input_tokens", 0)
        span.set_attribute("gen_ai.usage.output_tokens", 0)
        span.set_attribute("gen_ai.usage.cache_read_input_tokens", 1000)

    (_, model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    assert model == "claude-3-opus-20240229"
    assert bucket.calls == 1
    assert bucket.cache_read_tokens == 1000
    assert bucket.input_tokens == 0


from aight import auto


def test_auto_instrument_skips_providers_without_the_instrumentor_installed(monkeypatch):
    # Force the "not installed" path deterministically, regardless of
    # whether opentelemetry-instrumentation-openai/-anthropic (optional
    # extras) happen to be present in whoever's environment runs this test.
    def _always_import_error(name, *args, **kwargs):
        raise ImportError(name)

    monkeypatch.setattr(auto.importlib, "import_module", _always_import_error)
    monkeypatch.setattr(auto, "_instrumented", set())

    instrumented = auto.auto_instrument(["openai", "anthropic"])
    assert instrumented == []
