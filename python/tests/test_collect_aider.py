import json
from datetime import UTC, datetime

import pytest

# Same split as the Claude Code collector's tests: `push`, `whoami` and the
# marker belong to the shared module, and patching a name onto the parser module
# instead would leave a test calling the real network — or passing because a
# stub the run never consults looks exactly like a stub that did its job.
from aight.collect import aider
from aight.collect import base as collector
from aight.collect.aider import calls_from_log, main, rows_for

NOW = int(datetime.now(UTC).timestamp())


def _message_send(prompt_tokens=10, completion_tokens=5, model="openai/gpt-4o",
                  when=None, cost=0.0, total_cost=0.0, total_tokens=None):
    """One `message_send` event, as Aider writes it: an envelope around a
    properties object, with the time as an integer count of Unix seconds."""
    return {
        "event": "message_send",
        "properties": {
            "main_model": model,
            "edit_format": "diff",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens if total_tokens is not None
            else prompt_tokens + completion_tokens,
            "cost": cost,
            "total_cost": total_cost,
        },
        "user_id": "anon",
        "time": NOW if when is None else when,
    }


def _write(tmp_path, entries):
    path = tmp_path / "aider-analytics.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries))
    return path


def _rows_from_stdout(out):
    """The rows --dry-run prints, past the summary line above them."""
    return json.loads(out[out.index("["):])


@pytest.fixture(autouse=True)
def _no_marker_and_no_log_from_this_machine(tmp_path, monkeypatch):
    """For every test here. The marker would otherwise be the developer's real
    one, and AIDER_ANALYTICS_LOG is a variable this module's own code reads: a
    developer who has it set would get a different answer from the same test."""
    monkeypatch.setattr(aider, "MARKER_PATH", tmp_path / "marker.json")
    monkeypatch.delenv("AIDER_ANALYTICS_LOG", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """None is what the collector sees from a server without
    /api/ingest/whoami: the check is skipped and the run proceeds. Without it,
    every test that reaches a push makes a live HTTPS request whose answer
    changes when the deployment does."""
    monkeypatch.setattr(collector, "whoami", lambda key, url: None)


def _recording_push(monkeypatch):
    """(rows, badge) per push, instead of a request to the real endpoint."""
    monkeypatch.setenv("AIGHT_API_KEY", "test-key")
    sent: list[tuple[list[dict], str]] = []
    monkeypatch.setattr(collector, "push",
                        lambda rows, key, url, language: sent.append((rows, language)))
    return sent


def test_without_a_path_the_run_says_why_and_stops(capsys):
    """The most valuable message in this module. Aider writes nothing unless it
    was started with --analytics-log, and a session that already ran without it
    cannot be backfilled. Collecting zero files and exiting quietly would read
    exactly like a working collector on a quiet day, so the user would believe
    their Aider spend is being tracked when nothing has ever been recorded."""
    assert main([]) == 1

    err = capsys.readouterr().err
    assert "--analytics-log" in err
    assert "AIDER_ANALYTICS_LOG" in err
    assert "cannot be collected" in err


def test_the_environment_variable_is_the_default_root(tmp_path, monkeypatch, capsys):
    """Aider's log is a file at a path a person chose, so there is no directory
    to default to — the variable is the only default there is. The file is
    named for whatever the person called it, so the root is read as a file
    rather than matched against *.jsonl."""
    path = tmp_path / "wherever-aider-put-it.log"
    path.write_text(json.dumps(_message_send()))
    monkeypatch.setenv("AIDER_ANALYTICS_LOG", str(path))

    assert main(["--dry-run"]) == 0

    rows = _rows_from_stdout(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["chain"][0]["filepath"] == "aider"


def test_only_message_send_is_a_bill(tmp_path):
    path = _write(tmp_path, [
        {"event": "launch", "properties": {}, "user_id": "anon", "time": NOW},
        _message_send(prompt_tokens=10, completion_tokens=5),
        {"event": "exit", "properties": {}, "user_id": "anon", "time": NOW},
    ])

    calls = calls_from_log(path)

    assert len(calls) == 1
    assert calls[0].input_tokens == 10


def test_time_is_read_as_unix_seconds(tmp_path):
    """Aider's `time` is an integer count of seconds, not an ISO string — so it
    is Aider's own reader, and the boundary the resume is kept in is the same
    unit. Read as anything else, either the call is never new enough to send or
    every run sends the whole log again, which doubles it."""
    path = _write(tmp_path, [
        _message_send(prompt_tokens=10, when=NOW - 300),
        _message_send(prompt_tokens=20, when=NOW),
    ])

    calls = calls_from_log(path)

    assert [call.timestamp for call in calls] == [NOW - 300, NOW]
    assert [call.input_tokens for call in calls_from_log(path, since=NOW - 300)] == [20]


def test_an_undated_event_is_kept_rather_than_dropped(tmp_path):
    """A time this cannot read is not a time before the cutoff. Dropping calls
    we can't read is the worse error."""
    entry = _message_send()
    entry["time"] = "2026-09-25T10:00:00Z"  # not the shape Aider writes
    path = _write(tmp_path, [entry])

    calls = calls_from_log(path, since=NOW + 10_000)

    assert len(calls) == 1
    assert calls[0].timestamp == 0.0


def test_aider_reports_no_cache_tokens_so_both_counts_are_zero(tmp_path):
    """Aider's log has no cache fields at all. A zero here means *not
    reported*, not *nothing was cached* — a cache-heavy session is
    under-reported on those axes and nothing in the log says which it is."""
    path = _write(tmp_path, [_message_send(prompt_tokens=1000, completion_tokens=50)])

    call = calls_from_log(path)[0]

    assert call.cache_read_tokens == 0
    assert call.cache_creation_tokens == 0
    # Not folded into the prompt either: the prompt is what Aider counted.
    assert call.input_tokens == 1000


def test_one_message_is_one_call_however_many_round_trips_it_took(tmp_path):
    """`prompt_tokens` accumulates across every LLM call a message took: the
    message goes out, a tool call comes back, the result goes out again. The
    tokens are right and the call *count* is not — one row, calls 1."""
    path = _write(tmp_path, [_message_send(prompt_tokens=5000, completion_tokens=100)])

    calls = calls_from_log(path)
    rows = rows_for(calls)

    assert len(calls) == 1
    assert calls[0].steps == [("reply", "")]
    assert len(rows) == 1
    assert rows[0]["calls"] == 1
    assert rows[0]["input_tokens"] == 5000
    assert rows[0]["output_tokens"] == 100


def test_aiders_own_money_figures_are_not_sent(tmp_path):
    """`cost` and `total_cost` sit right beside the token counts. They are
    Aider's arithmetic, priced against a table this collector cannot see, and
    the platform prices every row at receive time from its own — so sending
    them would be a second opinion about money, arriving at a number that is
    already frozen."""
    path = _write(tmp_path, [_message_send(cost=0.42, total_cost=9.99)])

    row = rows_for(calls_from_log(path))[0]

    assert row["cost_usd"] == 0.0
    # The record carried money in it; the row carries none of it, under any
    # other name.
    assert "cost" not in row
    assert "total_cost" not in row


def test_a_record_with_only_a_total_has_no_split_to_price(tmp_path):
    """`total_tokens` is prompt plus completion, and prompt and completion are
    priced at different rates. Splitting a total by guess is inventing the
    split; the row is dropped instead."""
    path = _write(tmp_path, [
        {**_message_send(prompt_tokens=0, completion_tokens=0, total_tokens=1234)},
    ])

    assert calls_from_log(path) == []


def test_the_model_name_is_passed_through_unchanged(tmp_path):
    """Aider writes `main_model` through its own redaction, so the name here is
    whatever survived that. The platform prices by the exact string, which
    makes a redacted or aliased name an unpriced row — visible — rather than a
    guessed price, which is not."""
    path = _write(tmp_path, [_message_send(model="openai/REDACTED")])

    assert calls_from_log(path)[0].model == "openai/REDACTED"


def test_a_record_with_no_model_is_dropped(tmp_path):
    path = _write(tmp_path, [{**_message_send(model="")}])

    assert calls_from_log(path) == []


def test_a_partially_written_line_is_skipped(tmp_path):
    """The log is open for writing while a session runs, so a trailing partial
    line is normal. One unparseable line must not cost the file its spend."""
    path = tmp_path / "aider-analytics.jsonl"
    path.write_text(json.dumps(_message_send()) + "\n{\"event\": \"messa")

    assert len(calls_from_log(path)) == 1


def test_rows_are_external_rooted_at_aider_and_never_priced(tmp_path):
    path = _write(tmp_path, [_message_send(prompt_tokens=1000, completion_tokens=50)])

    row = rows_for(calls_from_log(path))[0]

    assert row["kind"] == "external"
    assert row["chain"][0] == {"filepath": "aider", "lineno": 0, "function": "aider"}
    assert row["chain"][1] == {"filepath": "", "lineno": 0, "function": "reply"}
    assert row["model"] == "openai/gpt-4o"
    assert row["cost_usd"] == 0.0


def test_the_push_carries_aider_as_the_badge(tmp_path, monkeypatch):
    """The badge is how the workspace tells one agent's rows from another's, and
    a collector that sent the wrong one would file its spend under an agent that
    did not make the calls."""
    sent = _recording_push(monkeypatch)
    path = _write(tmp_path, [_message_send()])

    assert main(["--root", str(path)]) == 0

    assert len(sent) == 1
    assert sent[0][1] == "aider"


def test_the_second_run_sends_nothing(tmp_path, monkeypatch, capsys):
    """The resume boundary is Aider's integer seconds, and it has to hold the
    same way it does for every other collector: a push of the same call adds
    again, and cost is frozen at receive time, so re-sending is permanent."""
    sent = _recording_push(monkeypatch)
    path = _write(tmp_path, [
        _message_send(prompt_tokens=10, when=NOW - 3 * 24 * 60 * 60),
        _message_send(prompt_tokens=20, when=NOW),
    ])

    # Nothing to resume from, so the first run reaches a day back: the call
    # from three days ago is out.
    assert main(["--root", str(path)]) == 0
    assert [row["input_tokens"] for row in sent[0][0]] == [20]

    # The marker says that call was sent, so the second run sends nothing.
    assert main(["--root", str(path)]) == 0
    assert len(sent) == 1
    assert "Nothing new to push" in capsys.readouterr().out

    # --all-time is the explicit re-push, and the only thing that reaches past
    # the marker.
    assert main(["--root", str(path), "--all-time"]) == 0
    assert sum(row["input_tokens"] for row in sent[1][0]) == 30
