import json
from datetime import UTC, datetime

import pytest

# The same split as the Claude Code collector's tests: `push`, `whoami` and the
# marker belong to the shared module, and patching a name onto the parser
# module instead would leave a test calling the real network — or, worse,
# passing because a stub the run never consults looks exactly like a stub that
# did its job.
from aight.collect import aider, claude_code, codex, gemini
from aight.collect import base as collector
from aight.collect.codex import calls_from_rollout, main, rows_for

# Read at import, before the fixture below points codex.MARKER_PATH at tmp_path:
# these are the paths the modules really ship with, which is the thing the
# marker test is about.
SHIPPED_MARKERS = {
    claude_code.MARKER_PATH,
    codex.MARKER_PATH,
    gemini.MARKER_PATH,
    aider.MARKER_PATH,
}


def _line(kind, payload, timestamp="2026-09-25T10:00:00.000Z"):
    return {"timestamp": timestamp, "ordinal": 0, "type": kind, "payload": payload}


def _turn_context(model="gpt-5-codex"):
    """Codex's model name, which lives here and nowhere else."""
    return _line("turn_context", {"model": model, "turn_id": "turn_1"})


def _session_meta():
    """The record that carries everything except the model, and which Codex
    writes again on every resume."""
    return _line("session_meta", {
        "id": "session-1",
        "cwd": "/home/dev/project",
        "model_provider": "openai",
        "git": {"branch": "main"},
    })


def _token_count(input_tokens=10, cached=0, output=5, total=None,
                 timestamp="2026-09-25T10:00:01.000Z"):
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": total if total is not None else input_tokens + output,
    }
    return _line(
        "event_msg",
        # The cumulative figure is the same object here, because there is
        # nothing to tell apart yet. The test that does tell them apart gives
        # them different numbers.
        {"type": "token_count", "info": {"last_token_usage": usage,
                                         "total_token_usage": usage}},
        timestamp=timestamp,
    )


def _usage_record(response_id, usage, timestamp="2026-09-25T10:00:02.000Z"):
    return _line("token_usage_record", {"response_id": response_id, "usage": usage},
                 timestamp=timestamp)


def _usage(input_tokens=10, cached=0, output=5, write=0):
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": write,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": input_tokens + output,
    }


def _write(tmp_path, entries):
    path = tmp_path / "sessions" / "2026" / "09" / "25" / "rollout-abc.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries))
    return path


def _rows_from_stdout(out):
    """The rows --dry-run prints, past the summary line above them."""
    return json.loads(out[out.index("["):])


@pytest.fixture(autouse=True)
def _no_marker_from_this_machine(tmp_path, monkeypatch):
    """For every test here: the collector consults the marker before it reads a
    rollout, so without this whether a test passes depends on whether the
    developer has ever run the collector on this machine."""
    monkeypatch.setattr(codex, "MARKER_PATH", tmp_path / "marker.json")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """None is exactly what the collector sees from a server without
    /api/ingest/whoami: the check is skipped and the run proceeds. Without
    this, every test that reaches a push makes a live HTTPS request to
    whatever the ingest URL resolves to, and the answer changes when the
    deployment does."""
    monkeypatch.setattr(collector, "whoami", lambda key, url: None)


def _recording_push(monkeypatch):
    """(rows, badge) per push, instead of a request to the real endpoint."""
    monkeypatch.setenv("AIGHT_API_KEY", "test-key")
    sent: list[tuple[list[dict], str]] = []
    monkeypatch.setattr(collector, "push",
                        lambda rows, key, url, language: sent.append((rows, language)))
    return sent


def test_cached_input_is_a_subset_of_the_prompt_not_beside_it(tmp_path):
    """Codex's `cached_input_tokens` is already counted inside `input_tokens`.

    AIght's `input_tokens` is the uncached prompt. Passing Codex's number
    through — the intuitive read, and the Anthropic convention — puts the
    cached tokens in both fields, and the platform prices both. That is
    permanently wrong money: ingest adds what it receives and cost is frozen at
    receive time, so there is no repair path."""
    path = _write(tmp_path, [_turn_context(), _token_count(input_tokens=1000, cached=800)])

    call = calls_from_rollout(path)[0]

    assert call.input_tokens == 200
    assert call.cache_read_tokens == 800
    # Together they are exactly what the provider counted — the check that the
    # subtraction is a re-split and not a loss.
    assert call.input_tokens + call.cache_read_tokens == 1000


def test_a_cache_write_is_taken_out_of_the_prompt_too(tmp_path):
    path = _write(tmp_path, [
        _turn_context(),
        _line("event_msg", {"type": "token_count", "info": {"last_token_usage": _usage(
            input_tokens=1000, cached=100, output=5, write=300)}}),
    ])

    call = calls_from_rollout(path)[0]

    assert call.cache_creation_tokens == 300
    assert call.input_tokens == 600
    assert call.input_tokens + call.cache_read_tokens + call.cache_creation_tokens == 1000


def test_a_prompt_smaller_than_its_cache_never_goes_negative(tmp_path):
    """A provider reporting more cached than input is a shape this code does not
    understand. A negative count would be sent as-is and nothing downstream is
    prepared to price one."""
    path = _write(tmp_path, [_turn_context(), _token_count(input_tokens=100, cached=900)])

    assert calls_from_rollout(path)[0].input_tokens == 0


def test_the_per_response_usage_is_read_and_not_the_running_total(tmp_path):
    """`info.total_token_usage` sits right beside `info.last_token_usage` in the
    same object. It is the session's running total, so reading it multiplies
    the spend by the number of turns taken so far — and a long session would
    look like it cost a hundred times what it did."""
    running = _usage(input_tokens=1000)
    entry = _line("event_msg", {"type": "token_count", "info": {
        "last_token_usage": _usage(input_tokens=10, output=5),
        "total_token_usage": running,
    }})
    path = _write(tmp_path, [_turn_context(), entry])

    call = calls_from_rollout(path)[0]

    assert call.input_tokens == 10
    assert call.output_tokens == 5


def test_the_model_comes_from_turn_context_not_session_meta(tmp_path):
    """`session_meta` carries the session id, the cwd, the provider and the git
    state, and no model at all. A parser reading the model off it prices every
    row against nothing."""
    assert "model" not in _session_meta()["payload"]
    path = _write(tmp_path, [
        _session_meta(),
        _turn_context("gpt-5-codex"),
        _token_count(),
    ])

    assert [call.model for call in calls_from_rollout(path)] == ["gpt-5-codex"]


def test_a_resume_is_the_same_session_and_the_model_carries_forward(tmp_path):
    """Codex writes `session_meta` again when a session is resumed, so a second
    one is a continuation, not a second session. The model lives on
    `turn_context`, which is not necessarily re-emitted — so it has to be
    carried forward or the resumed turn's calls have no model and vanish."""
    path = _write(tmp_path, [
        _session_meta(),
        _turn_context("gpt-5-codex"),
        _token_count(),
        _session_meta(),  # the resume
        _token_count(input_tokens=20, timestamp="2026-09-25T10:05:01.000Z"),
    ])

    calls = calls_from_rollout(path)

    assert [call.model for call in calls] == ["gpt-5-codex", "gpt-5-codex"]
    assert sum(call.input_tokens for call in calls) == 30


def test_a_turn_context_naming_a_new_model_applies_from_there_on(tmp_path):
    path = _write(tmp_path, [
        _turn_context("gpt-5-codex"),
        _token_count(),
        _turn_context("gpt-5-mini"),
        _token_count(timestamp="2026-09-25T10:06:00.000Z"),
    ])

    assert [call.model for call in calls_from_rollout(path)] == ["gpt-5-codex", "gpt-5-mini"]


def test_a_usage_record_before_any_model_is_dropped_rather_than_guessed(tmp_path):
    path = _write(tmp_path, [_token_count()])

    assert calls_from_rollout(path) == []


def test_a_tool_call_is_not_joined_to_the_usage_that_paid_for_it(tmp_path):
    """`function_call` carries a `call_id`; the usage record carries a
    `response_id`; nothing joins the two. Ordering them within a turn is a
    guess dressed as a parse, and a wrong one charges one file for another
    file's tokens. So the step is the session, and it is the same for every
    usage record — a resolution loss, made on purpose."""
    path = _write(tmp_path, [
        _session_meta(),
        _turn_context(),
        _line("response_item", {"type": "function_call", "name": "apply_patch",
                                "arguments": "{}", "call_id": "call_1"}),
        _line("response_item", {"type": "function_call_output", "call_id": "call_1",
                                "output": "done"}),
        _token_count(),
    ])

    calls = calls_from_rollout(path)

    assert [call.steps for call in calls] == [[("reply", "")]]
    rows = rows_for(calls)
    assert rows[0]["chain"][1] == {"filepath": "", "lineno": 0, "function": "reply"}


def test_a_file_with_both_usage_envelopes_counts_each_call_once(tmp_path):
    """v0.153+ writes the newer `token_usage_record`; the older `event_msg`
    envelope is still in the file. Reading both would double every call in it,
    permanently — so the newer one wins for the whole file."""
    path = _write(tmp_path, [
        _turn_context(),
        _token_count(input_tokens=10),
        _usage_record("resp_1", _usage(input_tokens=10)),
    ])

    calls = calls_from_rollout(path)

    assert len(calls) == 1
    assert calls[0].input_tokens == 10


def test_a_file_with_only_the_older_envelope_still_counts(tmp_path):
    """The fallback matters as much as the preference: every rollout written
    before v0.153 has only the old envelope, and reading nothing from those
    would report nothing for every older session on the machine."""
    path = _write(tmp_path, [_turn_context(), _token_count(input_tokens=10)])

    assert [call.input_tokens for call in calls_from_rollout(path)] == [10]


def test_a_repeated_response_id_is_counted_once(tmp_path):
    path = _write(tmp_path, [
        _turn_context(),
        _usage_record("resp_1", _usage(input_tokens=10)),
        _usage_record("resp_1", _usage(input_tokens=10),
                      timestamp="2026-09-25T10:00:03.000Z"),
        _usage_record("resp_2", _usage(input_tokens=20),
                      timestamp="2026-09-25T10:00:04.000Z"),
    ])

    assert [call.input_tokens for call in calls_from_rollout(path)] == [10, 20]


def test_unknown_record_types_are_skipped_rather_than_fatal(tmp_path):
    """`type` is snake_case and the enum has grown four variants across
    v0.150 → v0.157. A parser that raised on an unknown one would lose the
    whole file's spend, and the file is the only record there is."""
    path = _write(tmp_path, [
        _turn_context(),
        _line("some_future_event", {"anything": {"nested": [1, 2]}}),
        _line("response_item", "not even an object"),
        _token_count(),
        _line("another_future_event", None),
    ])

    assert len(calls_from_rollout(path)) == 1


def test_since_is_exclusive_and_per_record(tmp_path):
    """The cutoff can be a call an earlier run already pushed, so it must not
    come round again — and it is applied per record, because one rollout file
    spans the boundary."""
    path = _write(tmp_path, [
        _turn_context(),
        _token_count(input_tokens=10, timestamp="2026-09-25T09:00:00.000Z"),
        _token_count(input_tokens=20, timestamp="2026-09-25T10:00:00.000Z"),
        _token_count(input_tokens=30, timestamp="2026-09-25T11:00:00.000Z"),
    ])
    cutoff = datetime(2026, 9, 25, 10, 0, tzinfo=UTC).timestamp()

    assert [call.input_tokens for call in calls_from_rollout(path, since=cutoff)] == [30]


def test_rows_are_external_rooted_at_codex_and_never_priced(tmp_path):
    path = _write(tmp_path, [_turn_context(), _token_count(input_tokens=1000, cached=800)])

    row = rows_for(calls_from_rollout(path))[0]

    assert row["kind"] == "external"
    assert row["chain"][0] == {"filepath": "codex", "lineno": 0, "function": "codex"}
    assert row["model"] == "gpt-5-codex"
    # The money-path invariants: the collector offers no number of its own, and
    # the cache tokens are not also inside input_tokens.
    assert row["cost_usd"] == 0.0
    assert row["input_tokens"] == 200
    assert row["cache_read_tokens"] == 800


def test_the_push_carries_codex_as_the_badge(tmp_path, monkeypatch):
    """The badge is how the workspace tells one agent's rows from another's, and
    a collector that sent the wrong one would file its spend under an agent
    that did not make the calls."""
    sent = _recording_push(monkeypatch)
    path = _write(tmp_path, [_turn_context(), _token_count()])

    assert main(["--root", str(path)]) == 0

    assert len(sent) == 1
    assert sent[0][1] == "codex"


def test_codex_home_moves_the_default_root(tmp_path, monkeypatch, capsys):
    """`CODEX_HOME` is how Codex itself is pointed elsewhere, so a machine with
    it set keeps its rollouts somewhere a hardcoded ~/.codex would never look —
    and the run would report an empty, honest, wrong zero."""
    path = _write(tmp_path, [_turn_context(), _token_count()])
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    assert codex._sessions_root() == tmp_path / "sessions"
    # No --root: the run has to find the file from CODEX_HOME alone.
    assert main(["--dry-run"]) == 0

    rows = _rows_from_stdout(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["chain"][0]["filepath"] == "codex"
    assert path.exists()


def test_the_first_run_window_keeps_old_rollouts_out(tmp_path, capsys):
    """Same bounded first run as every collector: a file with nothing to resume
    from is read through a 24h window, never from the beginning of time, because
    ingest adds what it receives and cost is frozen at receive time — a
    full-history first run would double every figure already recorded."""
    path = _write(tmp_path, [
        _turn_context(),
        _token_count(input_tokens=10, timestamp="2020-01-01T00:00:00.000Z"),
        _token_count(input_tokens=20, timestamp=datetime.now(UTC).isoformat()),
    ])

    assert main(["--root", str(path), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "nothing pushed yet, so only calls from the last 24h" in out
    rows = _rows_from_stdout(out)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 20


def test_every_collector_has_its_own_marker_file():
    """Two agents cover different transcripts, so a shared marker would make
    each collector's files look like first runs to the other — and a first run
    reaches back 24h and re-pushes what it finds, which the API adds to what it
    already holds."""
    assert len(SHIPPED_MARKERS) == 4, SHIPPED_MARKERS
    # In the home directory rather than the working one: a marker that lives in
    # the cwd re-opens every file's first-run window the moment the tool is run
    # from somewhere else.
    assert all(path.parent == collector.MARKER_DIR for path in SHIPPED_MARKERS)
