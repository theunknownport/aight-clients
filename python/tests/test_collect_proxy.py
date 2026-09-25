"""The proxy's token arithmetic — the part where being wrong costs money.

Nothing here starts a server. What can silently ruin a customer's figures is
the conversion between each provider's usage shape and AIght's, and that is
plain data in and data out; the request path around it is exercised end to end
against a live server instead, because a fake one would only prove the fake.
"""
import json

import pytest

from aight.collect.proxy import (
    include_usage,
    model_from_sse,
    row_for,
    usage_from_anthropic,
    usage_from_openai,
    usage_from_sse,
)

# --- OpenAI: the subtraction that must happen -------------------------------

def test_openai_cached_tokens_are_removed_from_the_prompt():
    """`prompt_tokens` counts the cached prefix; AIght's `input_tokens` does
    not. Sending the inclusive number bills the cached tokens twice, and cost
    is frozen at receive time, so the error is permanent rather than corrected
    on the next push. This is the most expensive line in the module."""
    usage = usage_from_openai({
        "usage": {
            "prompt_tokens": 10_000,
            "completion_tokens": 250,
            "prompt_tokens_details": {"cached_tokens": 9_600},
        }
    })
    assert usage["input_tokens"] == 400, "the cached prefix was not removed"
    assert usage["cache_read_tokens"] == 9_600, "the cached count was dropped entirely"
    assert usage["output_tokens"] == 250
    # OpenAI charges nothing to write the cache; only Anthropic does.
    assert usage["cache_creation_tokens"] == 0
    # The invariant that makes the split safe: nothing invented, nothing lost.
    assert usage["input_tokens"] + usage["cache_read_tokens"] == 10_000


def test_openai_without_cache_details_is_passed_through_whole():
    """Most responses carry no `prompt_tokens_details` at all, and a missing
    cache figure means zero cached tokens — not a reason to drop the row."""
    usage = usage_from_openai({"usage": {"prompt_tokens": 120, "completion_tokens": 8}})
    assert usage["input_tokens"] == 120
    assert usage["cache_read_tokens"] == 0


def test_openai_cached_greater_than_prompt_is_clamped_not_negative():
    """No provider should report this, and if one does, a negative token count
    reaches a pricing table that has no idea what to do with it."""
    usage = usage_from_openai({
        "usage": {"prompt_tokens": 100, "completion_tokens": 1,
                  "prompt_tokens_details": {"cached_tokens": 500}}
    })
    assert usage["input_tokens"] == 0


@pytest.mark.parametrize("body", [
    {},
    {"usage": {}},
    {"usage": {"prompt_tokens": 0, "completion_tokens": 0}},
    {"error": {"message": "bad key"}},
])
def test_openai_reports_nothing_rather_than_a_row_of_zeros(body):
    """A row of zeros is a claim that the call was free. An error body, or a
    response with no usage, has to produce no row at all."""
    assert usage_from_openai(body) is None


# --- Anthropic: the subtraction that must NOT happen ------------------------

def test_anthropic_is_read_as_reported_with_no_subtraction():
    """Anthropic already reports the uncached prompt. Applying the OpenAI
    correction here — the easy mistake, since it is a different convention
    behind a similar field name — would take the cache reads off the prompt a
    second time and under-report every cache-heavy call."""
    usage = usage_from_anthropic({
        "usage": {
            "input_tokens": 400,
            "output_tokens": 250,
            "cache_read_input_tokens": 9_600,
            "cache_creation_input_tokens": 1_200,
        }
    })
    assert usage["input_tokens"] == 400, "the prompt was altered; Anthropic needs no correction"
    assert usage["cache_read_tokens"] == 9_600
    assert usage["cache_creation_tokens"] == 1_200


def test_anthropic_reports_nothing_without_usage():
    assert usage_from_anthropic({}) is None
    assert usage_from_anthropic({"usage": {}}) is None


# --- Streaming --------------------------------------------------------------

def _sse(*events):
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def test_openai_stream_usage_comes_from_the_final_chunk():
    raw = _sse(
        {"model": "gpt-4o", "choices": [{"delta": {"content": "hi"}}]},
        {"model": "gpt-4o", "choices": []},
        {"model": "gpt-4o", "choices": [],
         "usage": {"prompt_tokens": 500, "completion_tokens": 20,
                   "prompt_tokens_details": {"cached_tokens": 400}}},
    ) + b"data: [DONE]\n\n"
    usage = usage_from_sse(raw, "openai")
    assert usage["input_tokens"] == 100
    assert usage["cache_read_tokens"] == 400
    assert model_from_sse(raw) == "gpt-4o"


def test_anthropic_stream_usage_is_merged_across_two_event_types():
    """Neither event is complete: message_start has the prompt and the cache
    counts, message_delta has the output. Taking only the last one seen — the
    obvious way to write this — loses the whole prompt on every call."""
    raw = _sse(
        {"type": "message_start",
         "message": {"model": "claude-sonnet-5",
                     "usage": {"input_tokens": 900, "output_tokens": 1,
                               "cache_read_input_tokens": 50}}},
        {"type": "content_block_delta", "delta": {"text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 130}},
    )
    usage = usage_from_sse(raw, "anthropic")
    assert usage["input_tokens"] == 900, "message_start's prompt was lost"
    assert usage["output_tokens"] == 130, "message_delta's output did not win"
    assert usage["cache_read_tokens"] == 50
    assert model_from_sse(raw) == "claude-sonnet-5"


def test_a_stream_with_no_usage_reports_nothing():
    raw = _sse({"model": "gpt-4o", "choices": [{"delta": {"content": "hi"}}]})
    assert usage_from_sse(raw, "openai") is None


def test_a_malformed_event_does_not_lose_the_rest_of_the_stream():
    """This runs inside somebody's request path, and half a stream still holds
    a usable count."""
    raw = (b"data: {not json at all\n\n"
           + _sse({"model": "gpt-4o", "usage": {"prompt_tokens": 7, "completion_tokens": 3}}))
    usage = usage_from_sse(raw, "openai")
    assert usage["input_tokens"] == 7


def test_model_from_sse_takes_the_first_name_not_the_last():
    """A stream that switched models mid-flight is attributed to the one that
    was asked for, which is the one the prompt was priced against."""
    raw = _sse({"model": "gpt-4o"}, {"model": "gpt-4o-mini"})
    assert model_from_sse(raw) == "gpt-4o"


# --- Request rewriting ------------------------------------------------------

def test_include_usage_adds_the_flag_a_streaming_call_needs():
    out = json.loads(include_usage(b'{"model":"gpt-4o","stream":true}'))
    assert out["stream_options"]["include_usage"] is True


def test_include_usage_preserves_options_the_client_already_set():
    out = json.loads(include_usage(b'{"stream":true,"stream_options":{"foo":1}}'))
    assert out["stream_options"] == {"foo": 1, "include_usage": True}


@pytest.mark.parametrize("body", [b"not json", b"[1,2,3]", b'"a string"', b"", b"null"])
def test_include_usage_declines_to_rewrite_what_it_cannot_parse(body):
    """None means forward untouched. A malformed body is the upstream's to
    reject, with its own error message, not ours to rewrite into a different
    one."""
    assert include_usage(body) is None


# --- Row shape --------------------------------------------------------------

def test_row_for_is_external_and_carries_the_latency():
    usage = {"input_tokens": 10, "output_tokens": 2,
             "cache_read_tokens": 0, "cache_creation_tokens": 0}
    row = row_for("codex", "/v1/chat/completions", "gpt-4o", usage, 812.5)

    assert row["kind"] == "external"
    assert row["chain"][0]["filepath"] == "codex"
    # Frame 1 carries the endpoint: there is no source line in an HTTP request,
    # which is why this is external rather than internal.
    assert row["chain"][1] == {"filepath": "", "lineno": 0,
                               "function": "/v1/chat/completions"}
    assert row["calls"] == 1
    # The proxy measures this itself, unlike the SDKs, which record after the
    # call returns and would only be timing the recording.
    assert row["latency_ms"] == 812.5
    # Never priced here: the platform's table is definitive.
    assert row["cost_usd"] == 0.0
    assert row["input_tokens"] == 10
