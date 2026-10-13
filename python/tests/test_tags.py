"""Span tags and the event's agent/tags: the client half of matching a
business event to the agent that produced it."""
import json
import warnings
from unittest.mock import patch

import pytest

import aight.tracing as aight
from aight.remote import push, push_event
from aight.tracing import COST_PROCESSOR, traced_llm_call


class _Resp:
    def __init__(self, body=b'{"ingested": 1, "results": []}'):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(fn, *args, **kwargs):
    sent = []

    def fake_urlopen(request, timeout=10):
        sent.append(json.loads(request.data))
        return _Resp()

    with patch("aight.remote.urllib.request.urlopen", side_effect=fake_urlopen):
        fn(*args, **kwargs)
    return sent[0] if sent else None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AIGHT_API_KEY", "aight_test_key")
    COST_PROCESSOR.buckets.clear()
    aight._WARNED.clear()


def _call():
    traced_llm_call("gpt-4o-mini", input_tokens=10, output_tokens=5)


def test_tags_land_on_rows_inside_the_scope_and_not_outside():
    with aight.tags(order_id="o_123", n=7):
        _call()
    _call()
    rows = _capture(push, COST_PROCESSOR.buckets)
    tagged = [r for r in rows if "tags" in r]
    untagged = [r for r in rows if "tags" not in r]
    assert [r["tags"] for r in tagged] == [{"order_id": "o_123", "n": "7"}]  # numbers stringified
    assert len(untagged) == 1


def test_nested_scopes_merge_and_inner_wins():
    with aight.tags(order_id="o_1", user_id="u_9"):
        with aight.tags({"order_id": "o_2", "plan": "pro"}):
            assert aight.current_tags() == {"order_id": "o_2", "user_id": "u_9", "plan": "pro"}
        assert aight.current_tags() == {"order_id": "o_1", "user_id": "u_9"}
    assert aight.current_tags() == {}


def test_limits_drop_and_warn_once_never_raise():
    too_many = {f"k{i}": "v" for i in range(20)}
    with pytest.warns(UserWarning, match="more than 16") as record:
        assert len(aight.clean_tags(too_many)) == 16
    with warnings.catch_warnings(record=True) as again:
        warnings.simplefilter("always")
        aight.clean_tags(too_many)
    assert len(record) == 1 and again == [], "warned once, not per call"
    with pytest.warns(UserWarning):
        cleaned = aight.clean_tags({"k" * 65: "v", "ok": "x" * 257, "fine": "x" * 256})
    assert cleaned == {"fine": "x" * 256}
    with aight.tags(too_many):  # the scope applies the same limits
        assert len(aight.current_tags()) == 16


def test_differently_tagged_calls_on_one_line_stay_separate_rows():
    for order in ("o_1", "o_2", "o_1"):
        with aight.tags(order_id=order):
            _call()
    rows = _capture(push, COST_PROCESSOR.buckets)
    by_order = {r["tags"]["order_id"]: r["calls"] for r in rows}
    assert by_order == {"o_1": 2, "o_2": 1}


def test_push_event_sends_agent_and_tags_only_when_set():
    bare = _capture(push_event, "signup")
    assert "agent" not in bare and "tags" not in bare
    full = _capture(push_event, "signup", agent="/app/agent.py", tags={"order_id": 5})
    assert full["agent"] == "/app/agent.py"
    assert full["tags"] == {"order_id": "5"}


def test_push_event_picks_up_the_scope_tags_unless_given_its_own():
    with aight.tags(order_id="o_123"):
        assert _capture(push_event, "checkout")["tags"] == {"order_id": "o_123"}
        assert _capture(push_event, "checkout", tags={"x": "y"})["tags"] == {"x": "y"}
