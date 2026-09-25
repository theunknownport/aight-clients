"""Tests for remote.push()'s wire format — verifies the row shape sent to
/api/ingest/spans without making a real network call."""
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from aight.remote import push, push_event, push_value
from aight.tracing import (
    COST_PROCESSOR,
    SNIPPETS,
    VALUE_BY_FILE,
    TraceContext,
    _Bucket,
    report_value,
    traced_llm_call,
)


class _FakeResponse:
    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_push_sends_chain_shape_not_flat_triple(monkeypatch):
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    # Keyed by (chain, model) — the model is part of the bucket identity.
    buckets = {
        ((("agent.py", 10, "outer"), ("agent.py", 42, "call_llm")), "gpt-4o-mini"): _Bucket(
            calls=2, input_tokens=100, output_tokens=40, latency_ms=250.0
        ),
    }

    captured = {}

    class FakeResponse:
        def read(self):
            return b""
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=10):
        import json
        captured["body"] = json.loads(request.data)
        return FakeResponse()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        push(buckets)

    rows = captured["body"]
    assert len(rows) == 1
    row = rows[0]
    assert row["calls"] == 2
    # No cost on the wire. The SDK has no price table (the Python one had
    # already drifted from the platform's and stopped repricing models the
    # server still carried); the row carries model + token counts and the
    # server prices it at receive.
    assert "cost_usd" not in row
    assert row["chain"] == [
        {"filepath": "agent.py", "lineno": 10, "function": "outer"},
        {"filepath": "agent.py", "lineno": 42, "function": "call_llm"},
    ]
    assert row["model"] == "gpt-4o-mini"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 40
    assert row["latency_ms"] == 250.0


def test_a_traced_call_pushes_a_row_with_no_cost(monkeypatch):
    """The contract, end to end: instrument a call, push it, and the row carries
    what the server needs to price it (model + four token counts) and no cost of
    its own. An absent cost_usd reads as "making no claim", which is what lets
    the platform's own table be the only one that ever prices a row."""
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    COST_PROCESSOR.buckets.clear()
    traced_llm_call(
        "claude-3-5-sonnet-20241022",
        input_tokens=180,
        output_tokens=60,
        cache_read_tokens=500,
        cache_creation_tokens=20,
    )

    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["body"] = json.loads(request.data)
        return _FakeResponse()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        push(COST_PROCESSOR.buckets)

    row = captured["body"][0]
    assert "cost_usd" not in row
    assert row["model"] == "claude-3-5-sonnet-20241022"
    assert row["input_tokens"] == 180
    assert row["output_tokens"] == 60
    assert row["cache_read_tokens"] == 500
    assert row["cache_creation_tokens"] == 20


def test_push_carries_cache_tokens_as_their_own_counts(monkeypatch):
    """The gap this closes: the counts used to stop at the process boundary.
    `_Bucket` had no fields for them and push() rebuilt every row from a fixed
    list of keys, so a cache-bearing call under-reported cost through every SDK
    — against a server that already priced all four rates and froze the result
    at receive."""
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    buckets = {
        ((("agent.py", 10, "call_llm"),), "claude-3-opus-20240229"): _Bucket(
            calls=1,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=8000,
            cache_creation_tokens=3000,
        ),
    }

    captured = {}

    class FakeResponse:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=10):
        captured["body"] = json.loads(request.data)
        return FakeResponse()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        push(buckets)

    row = captured["body"][0]
    assert row["cache_read_tokens"] == 8000
    assert row["cache_creation_tokens"] == 3000
    # And input_tokens stays the *uncached* count. Folding the cache read in
    # here is exactly what would double-bill it, since the platform sums four
    # rates rather than treating the cache counts as a discount on one.
    assert row["input_tokens"] == 100


def test_push_attaches_a_known_snippet_to_its_frame(monkeypatch):
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    SNIPPETS[("agent.py", 42)] = (39, "line 39\nline 40\nline 41\nline 42\nline 43\nline 44\nline 45")
    buckets = {((("agent.py", 42, "call_llm"),), ""): _Bucket(calls=1)}

    captured = {}

    class FakeResponse:
        def read(self):
            return b""
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=10):
        import json
        captured["body"] = json.loads(request.data)
        return FakeResponse()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        push(buckets)

    frame = captured["body"][0]["chain"][0]
    assert frame["snippet_start"] == 39
    assert "line 42" in frame["snippet"]


def test_push_carries_the_trace_context_trace_id_from_a_real_traced_call(monkeypatch):
    """traced_llm_call() inside a TraceContext sets aight.trace_id on the
    span; CostByLineProcessor.on_end must actually pick that up onto the
    bucket, or push()'s payload has no trace_id and push_event()'s
    EXPLICIT match can never fire for spans sent through push()."""
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    COST_PROCESSOR.buckets.clear()
    with TraceContext(trace_id="ctx-trace-2"):
        traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=5)

    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["body"] = json.loads(request.data)
        return _FakeResponse()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        push(COST_PROCESSOR.buckets)

    assert captured["body"][0]["trace_id"] == "ctx-trace-2"


def test_push_clears_the_buckets_once_the_server_acknowledges(monkeypatch):
    """The ingest endpoint *adds* each row to what it already holds, so a
    second push of the same running totals would double-count every call."""
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    COST_PROCESSOR.buckets.clear()
    traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=5)
    assert COST_PROCESSOR.buckets

    def fake_urlopen(request, timeout=10):
        return _FakeResponse()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        push(COST_PROCESSOR.buckets)

    assert COST_PROCESSOR.buckets == {}


def test_push_keeps_the_buckets_when_the_push_fails(monkeypatch):
    """Clearing is only safe after an acknowledged push — losing unsent data
    to a network blip would be worse than the double-count it prevents."""
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    COST_PROCESSOR.buckets.clear()
    traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=5)

    def boom(request, timeout=10):
        raise OSError("network down")

    with patch("aight.remote.urllib.request.urlopen", side_effect=boom):
        with pytest.raises(OSError):
            push(COST_PROCESSOR.buckets)

    assert COST_PROCESSOR.buckets, "a failed push must not discard unsent data"


def test_push_value_clears_the_accumulator_once_the_server_acknowledges(monkeypatch):
    """report_value() only ever adds to VALUE_BY_FILE and nothing ever drained
    it, so passing it to push_value() — which is what the docstring tells you to
    do — re-sent every earlier earning on each flush. /api/ingest/value is a
    plain INSERT with no dedup, so the same closed deal was counted again and
    again and the inflation was permanent."""
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    VALUE_BY_FILE.clear()
    report_value(49.0)

    sent = []

    def fake_urlopen(request, timeout=10):
        sent.append(json.loads(request.data))
        return _FakeResponse()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        push_value(VALUE_BY_FILE)
        push_value(VALUE_BY_FILE)

    assert len(sent) == 1, "the second push had nothing left, so it must not send"
    assert sent[0][0]["value_usd"] == 49.0


def test_push_value_keeps_the_accumulator_when_the_push_fails(monkeypatch):
    """Clearing is only safe after an acknowledged push — losing unsent
    earnings to a network blip is worse than the double-count it prevents."""
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    VALUE_BY_FILE.clear()
    report_value(49.0)

    def boom(request, timeout=10):
        raise OSError("network down")

    with patch("aight.remote.urllib.request.urlopen", side_effect=boom):
        with pytest.raises(OSError):
            push_value(VALUE_BY_FILE)

    assert VALUE_BY_FILE, "a failed push must not discard unsent earnings"


def _http_error(request, code: int, body: bytes):
    return urllib.error.HTTPError(
        request.full_url, code, "Unauthorized", {}, io.BytesIO(body)
    )


def test_a_rejected_push_says_why(monkeypatch):
    """urllib's HTTPError carries the status and discards the body — but the
    ingest endpoints put the only actionable sentence in that body. Throwing it
    away is the difference between re-minting a key in five seconds and an
    afternoon reading container logs for a 401 that says "Unauthorized"."""
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    COST_PROCESSOR.buckets.clear()
    traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=5)

    def unauthorized(request, timeout=10):
        raise _http_error(request, 401, b'{"detail": "Unknown or revoked API key"}')

    with (
        patch("aight.remote.urllib.request.urlopen", side_effect=unauthorized),
        pytest.raises(RuntimeError) as exc,
    ):
        push(COST_PROCESSOR.buckets)

    assert "401" in str(exc.value)
    assert "Unknown or revoked API key" in str(exc.value)


def test_a_non_json_error_body_does_not_mask_the_status(monkeypatch):
    """A gateway in front of the API answers 502 with HTML, not the app's
    {"detail": ...}. Parsing that must not turn a status line into a traceback
    about JSON — the status is the useful part and it has to survive."""
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    COST_PROCESSOR.buckets.clear()
    traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=5)

    def bad_gateway(request, timeout=10):
        raise _http_error(request, 502, b"<html><body>Bad Gateway</body></html>")

    with (
        patch("aight.remote.urllib.request.urlopen", side_effect=bad_gateway),
        pytest.raises(RuntimeError) as exc,
    ):
        push(COST_PROCESSOR.buckets)

    assert "502" in str(exc.value)


def test_push_event_defaults_trace_id_to_active_trace_context(monkeypatch):
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["body"] = json.loads(request.data)
        return _FakeResponse(json.dumps({"ingested": 1, "results": [{"match_type": "EXPLICIT"}]}).encode())

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        with TraceContext(trace_id="ctx-trace-1"):
            result = push_event("checkout_completed", value=49.0)

    assert captured["body"]["event_name"] == "checkout_completed"
    assert captured["body"]["value"] == 49.0
    assert captured["body"]["trace_id"] == "ctx-trace-1"
    assert result["results"][0]["match_type"] == "EXPLICIT"


def test_push_event_explicit_trace_id_overrides_context(monkeypatch):
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["body"] = json.loads(request.data)
        return _FakeResponse()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        with TraceContext(trace_id="ctx-trace-1"):
            push_event("signup", trace_id="explicit-override")

    assert captured["body"]["trace_id"] == "explicit-override"


def test_push_event_with_no_context_sends_empty_trace_id(monkeypatch):
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["body"] = json.loads(request.data)
        return _FakeResponse()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        push_event("signup")

    assert captured["body"]["trace_id"] == ""
    assert captured["body"]["event_id"]  # auto-generated, non-empty
