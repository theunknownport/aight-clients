import re

from aight.tracing import COST_PROCESSOR, SNIPPETS, VALUE_BY_FILE, report_value, traced_llm_call
from examples.langgraph_spike import build_graph, run_spike
from _helper import report_from_helper


def test_resolves_full_chain_not_just_immediate_caller():
    COST_PROCESSOR.buckets.clear()

    def inner():
        return traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=10)

    def outer():
        return inner()

    outer()

    assert len(COST_PROCESSOR.buckets) == 1
    (chain, _model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    functions = [f[2] for f in chain]
    assert functions[-1] == "inner"  # leaf: the actual call site
    assert "outer" in functions      # the frame above it is still captured
    assert functions[0] == "outer"   # outer-to-leaf order: outer first


def test_chain_caps_at_five_frames_keeping_the_leaf_end():
    COST_PROCESSOR.buckets.clear()

    def L5():
        return traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=10)
    def L4(): return L5()
    def L3(): return L4()
    def L2(): return L3()
    def L1(): return L2()
    def L0(): return L1()

    L0()

    (chain, _model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    functions = [f[2] for f in chain]
    assert len(functions) == 5
    assert functions[-1] == "L5"   # leaf always kept
    assert "L0" not in functions   # outermost frame dropped to stay at cap


def test_resolves_user_code_line_not_framework():
    COST_PROCESSOR.buckets.clear()
    build_graph().invoke({"question": "q", "plan": "", "answer": ""})

    assert COST_PROCESSOR.buckets, "expected at least one attributed span"
    for (chain, _model), bucket in COST_PROCESSOR.buckets.items():
        filepath, lineno, _function = chain[-1]  # Get the leaf of the chain
        assert filepath.endswith("langgraph_spike.py"), f"attributed to framework code: {filepath}"
        assert lineno > 0
        assert bucket.calls >= 1


def test_report_prints_file_line_calls_and_tokens():
    COST_PROCESSOR.buckets.clear()
    report = run_spike(n_runs=1)
    # Each line names its model in brackets, so a call site that used two
    # models shows up as two separate rows rather than one blended one. No
    # dollar figure: the SDK ships no price table, the platform prices the rows.
    assert re.search(
        r"langgraph_spike\.py:\d+ \(\w+\)( \[[\w.\-]+\])? = \d+ call\(s\), \d+ in / \d+ out",
        report,
    )
    assert "$" not in report


def test_report_value_attributes_to_calling_file_and_accumulates():
    VALUE_BY_FILE.clear()
    report_value(120)
    report_value(30)

    assert len(VALUE_BY_FILE) == 1
    (filepath, value), = VALUE_BY_FILE.items()
    assert filepath.endswith("test_tracing.py")
    assert value == 150


def test_report_value_attributes_to_the_outer_agent_not_the_helper():
    """Spend is attributed to the outermost frame of the call chain, which is
    the file the fleet view groups agents by. Value has to land on that same
    file: resolving the *immediate caller* instead splits one agent in two
    whenever report_value is called from a helper — spend on the outer file,
    earnings on the helper — and the spent-vs-earned view never lines up."""
    VALUE_BY_FILE.clear()

    def agent_run():
        report_from_helper(49.0)

    agent_run()

    assert len(VALUE_BY_FILE) == 1
    (filepath, value), = VALUE_BY_FILE.items()
    assert filepath.endswith("test_tracing.py"), f"attributed to the helper instead: {filepath}"
    assert "_helper.py" not in filepath
    assert value == 49.0


def test_captures_a_few_lines_of_source_around_the_traced_line():
    COST_PROCESSOR.buckets.clear()
    SNIPPETS.clear()

    line_before_marker = "line before"           # noqa: F841 (line -1)
    traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=10)  # the traced line
    line_after_marker = "line after"             # noqa: F841 (line +1)

    (chain, _model), _bucket = next(iter(COST_PROCESSOR.buckets.items()))
    filepath, lineno, _function = chain[-1]

    start, text = SNIPPETS[(filepath, lineno)]
    assert start == lineno - 3
    assert "line before" in text
    assert "line after" in text
    assert text.count("\n") == 6  # 3 before + the call line + 3 after


def test_latency_is_recorded_only_when_the_caller_measured_it():
    """This SDK does not time the LLM call. traced_llm_call runs *after* the
    call returns, so the span's own duration is the cost of recording the
    numbers — a real two-second call recorded about 2ms. What lands on the
    bucket is now only ever the duration the caller passed in."""
    COST_PROCESSOR.buckets.clear()

    for latency in (2000.0, None):
        traced_llm_call("gpt-4o-mini", input_tokens=100, output_tokens=50, latency_ms=latency)

    (_chain, _model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    assert bucket.calls == 2
    assert bucket.latency_ms == 2000.0


def test_an_untimed_call_claims_no_latency_rather_than_a_tiny_one():
    """The bug this replaces: every call carried a ~2ms figure that was really
    how long the recording took. The platform reads 0 as "not measured" and
    shows "—" on the Speed axis, which is the honest answer for a call nobody
    timed."""
    COST_PROCESSOR.buckets.clear()
    traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=5)

    (_chain, _model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    assert bucket.latency_ms == 0


def test_bucket_accumulates_model_tokens_and_latency():
    COST_PROCESSOR.buckets.clear()

    for input_tokens, output_tokens in [(10, 5), (20, 8)]:
        traced_llm_call("gpt-4o-mini", input_tokens=input_tokens, output_tokens=output_tokens)

    (chain, model), bucket = next(iter(COST_PROCESSOR.buckets.items()))
    assert model == "gpt-4o-mini"
    assert chain[-1][2] == "test_bucket_accumulates_model_tokens_and_latency"
    assert bucket.calls == 2
    assert bucket.input_tokens == 30
    assert bucket.output_tokens == 13
    assert bucket.latency_ms >= 0


def test_one_line_calling_two_models_keeps_two_buckets():
    """The model is part of the bucket key, not a field on it. Merging them
    summed both models' tokens under whichever name was recorded last, and the
    server then repriced every cheap call at the expensive model's rate.

    Both calls are issued from the same line (inside the loop), so the call
    chains are identical and only the model distinguishes the two buckets."""
    COST_PROCESSOR.buckets.clear()

    for model in ("gpt-4o-mini", "gpt-4o"):
        traced_llm_call(model, input_tokens=1000, output_tokens=1000)

    assert len(COST_PROCESSOR.buckets) == 2
    by_model = {model: b for (_chain, model), b in COST_PROCESSOR.buckets.items()}
    assert set(by_model) == {"gpt-4o-mini", "gpt-4o"}
    # Each bucket holds only its own model's tokens, not both summed together.
    assert by_model["gpt-4o-mini"].input_tokens == 1000
    assert by_model["gpt-4o"].input_tokens == 1000
    # Which model is dearer is the platform's judgement now, not the SDK's —
    # both rows go over the wire with their own model name and let it price them.
