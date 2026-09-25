import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Same split as the Claude Code collector's tests: `push`, `whoami` and the
# marker belong to the shared module, and patching a name onto the parser module
# instead would leave a test calling the real network — or passing because a
# stub the run never consults looks exactly like a stub that did its job.
from aight.collect import base as collector
from aight.collect import gemini
from aight.collect.gemini import calls_from_session, main, rows_for


def _gemini(message_id, input_tokens=10, cached=0, output=5, model="gemini-2.5-pro",
            tool_calls=None, timestamp="2026-09-25T10:00:00.000Z"):
    entry = {
        "id": message_id,
        "type": "gemini",
        "timestamp": timestamp,
        "model": model,
        "tokens": {
            "input": input_tokens,
            "output": output,
            "cached": cached,
            "thoughts": 0,
            "tool": 0,
            "total": input_tokens + output,
        },
    }
    if tool_calls is not None:
        entry["toolCalls"] = tool_calls
    return entry


def _tool(name, args, status="success"):
    return {"id": f"call_{name}", "name": name, "args": args, "status": status,
            "timestamp": "2026-09-25T10:00:01.000Z"}


def _write(tmp_path, entries):
    path = tmp_path / "chats" / "session-2026-09-25.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries))
    return path


def _rows_from_stdout(out):
    """The rows --dry-run prints, past the summary line above them."""
    return json.loads(out[out.index("["):])


@pytest.fixture(autouse=True)
def _no_marker_from_this_machine(tmp_path, monkeypatch):
    """For every test here: the collector consults the marker before it reads a
    session, so without this whether a test passes depends on whether the
    developer has ever run the collector on this machine."""
    monkeypatch.setattr(gemini, "MARKER_PATH", tmp_path / "marker.json")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """None is what the collector sees from a server without
    /api/ingest/whoami: the check is skipped and the run proceeds. Without it,
    every test that reaches a push makes a live HTTPS request whose answer
    changes when the deployment does."""
    monkeypatch.setattr(collector, "whoami", lambda key, url: None)


def test_a_re_emitted_message_is_counted_once(tmp_path):
    """Gemini has no on-disk messages array: it writes the whole message again
    every time it changes, so a turn grows across lines. Counted per line, a
    session with three tool calls in one turn counts that turn four times — and
    ingest adds what it receives, so the double is permanent."""
    path = _write(tmp_path, [
        _gemini("msg_1", input_tokens=10, output=2),
        _gemini("msg_1", input_tokens=400, output=20, cached=300),
        _gemini("msg_1", input_tokens=400, output=90, cached=300),
    ])

    calls = calls_from_session(path)

    assert len(calls) == 1
    # Last one wins, and it is the last one's numbers — not the first, and not
    # a sum of the three.
    assert calls[0].output_tokens == 90
    assert calls[0].cache_read_tokens == 300


def test_a_record_with_no_id_is_dropped_rather_than_counted(tmp_path):
    """Without an id an update cannot be told from a new call, and counting
    both doubles the session. An empty collector is noticed; a doubled figure
    is not, and cannot be undone."""
    path = _write(tmp_path, [
        {**_gemini("msg_1"), "id": None},
        {"type": "gemini", "timestamp": "2026-09-25T10:00:00.000Z",
         "model": "gemini-2.5-pro", "tokens": {"input": 10, "output": 5}},
    ])

    assert calls_from_session(path) == []


def test_a_rewind_drops_the_branch_it_abandoned(tmp_path):
    """`$rewindTo msg_1` means everything after msg_1 was thrown away — the
    user edited an earlier prompt, or retried. Folding by id alone keeps those
    calls, and their spend was never real."""
    path = _write(tmp_path, [
        _gemini("msg_1", output=5),
        _gemini("msg_2", output=7),
        {"$rewindTo": "msg_1"},
        _gemini("msg_3", output=9),
    ])

    calls = calls_from_session(path)

    assert [call.output_tokens for call in calls] == [5, 9]


def test_a_rewind_naming_an_unknown_id_leaves_the_fold_alone(tmp_path):
    """A rewind names a message this file has. One that names nothing is not a
    reason to discard the session."""
    path = _write(tmp_path, [
        _gemini("msg_1", output=5),
        {"$rewindTo": "msg_nowhere"},
    ])

    assert len(calls_from_session(path)) == 1


def test_a_set_record_is_not_a_message(tmp_path):
    """`$set` patches bookkeeping — the session's last-updated stamp — and is
    skipped rather than read as a message. Even a $set that did carry tokens
    would be skipped: sending it would be inventing a call, while skipping it
    loses at most a row a parser fix and an --all-time run can put back."""
    path = _write(tmp_path, [
        _gemini("msg_1", output=5),
        {"$set": {"lastUpdated": "2026-09-25T10:01:00.000Z"}},
        {"$set": {"model": "gemini-2.5-pro", "tokens": {"input": 999, "output": 999}}},
    ])

    calls = calls_from_session(path)

    assert [call.output_tokens for call in calls] == [5]


def test_cached_is_a_subset_of_the_prompt_not_beside_it(tmp_path):
    """`input` is promptTokenCount, which counts the cached prefix as part of
    the prompt, and AIght's `input_tokens` is the uncached prompt. Passing the
    inclusive number through bills the cached tokens twice — permanently, since
    cost is frozen at receive time."""
    path = _write(tmp_path, [_gemini("msg_1", input_tokens=1000, cached=800, output=50)])

    call = calls_from_session(path)[0]

    assert call.input_tokens == 200
    assert call.cache_read_tokens == 800
    assert call.input_tokens + call.cache_read_tokens == 1000


def test_a_prompt_smaller_than_its_cache_never_goes_negative(tmp_path):
    path = _write(tmp_path, [_gemini("msg_1", input_tokens=100, cached=900)])

    assert calls_from_session(path)[0].input_tokens == 0


def test_gemini_reports_no_cache_writes_so_that_count_is_zero(tmp_path):
    """There is no creation count in these records. The zero means *not
    reported*, not *nothing was cached* — and copying the read count into it
    would invent a charge for a write Gemini never mentioned."""
    path = _write(tmp_path, [_gemini("msg_1", input_tokens=1000, cached=800)])

    assert calls_from_session(path)[0].cache_creation_tokens == 0


def test_tool_calls_on_the_record_become_its_steps(tmp_path):
    """The bill and the activities it paid for are one object, so there is no
    join to guess at. Both file shapes are read from the call's own arguments:
    `file_path` for the file tools, `command` for the shell."""
    path = _write(tmp_path, [_gemini("msg_1", input_tokens=10, output=5, tool_calls=[
        _tool("read_file", {"file_path": "src/aight/store.py"}),
        _tool("run_shell_command", {"command": "pytest -q"}),
        _tool("write_file", {"file_path": "src/b.py"}, status="error"),
    ])])

    call = calls_from_session(path)[0]

    # The failed call is a step too: it was billed whether or not it worked.
    assert call.steps == [
        ("read_file", "src/aight/store.py"),
        ("run_shell_command", "pytest -q"),
        ("write_file", "src/b.py"),
    ]
    rows = rows_for([call])
    assert sum(row["input_tokens"] for row in rows) == 10
    assert len(rows) == 3


def test_a_call_with_no_tools_is_a_single_reply_step(tmp_path):
    path = _write(tmp_path, [_gemini("msg_1", tool_calls=[])])

    assert calls_from_session(path)[0].steps == [("reply", "")]


def test_a_tool_call_with_no_readable_args_still_becomes_a_step(tmp_path):
    path = _write(tmp_path, [_gemini("msg_1", tool_calls=[
        _tool("some_future_tool", {}),
        "not even an object",
    ])])

    assert calls_from_session(path)[0].steps == [("some_future_tool", "")]


def test_a_record_with_no_tokens_is_not_a_call(tmp_path):
    """User turns and records with no model named sit in the same file. Neither
    can be priced, and a row of zeros is a claim that the call was free."""
    path = _write(tmp_path, [
        {"id": "u1", "type": "user", "timestamp": "2026-09-25T10:00:00.000Z",
         "content": "hello"},
        {**_gemini("msg_0"), "model": None},
        {**_gemini("msg_1"), "tokens": {}},
        _gemini("msg_2", input_tokens=0, cached=0, output=0),
        _gemini("msg_3", input_tokens=10),
    ])

    calls = calls_from_session(path)

    assert len(calls) == 1
    assert calls[0].model == "gemini-2.5-pro"


def test_since_is_exclusive_and_per_message(tmp_path):
    """The cutoff can be a call an earlier run already pushed, so it must not
    come round again — and it is applied per record, because one session file
    spans the boundary."""
    path = _write(tmp_path, [
        _gemini("msg_1", output=5, timestamp="2026-09-25T09:00:00.000Z"),
        _gemini("msg_2", output=7, timestamp="2026-09-25T10:00:00.000Z"),
        _gemini("msg_3", output=9, timestamp="2026-09-25T11:00:00.000Z"),
    ])
    cutoff = datetime(2026, 9, 25, 10, 0, tzinfo=UTC).timestamp()

    assert [call.output_tokens for call in calls_from_session(path, since=cutoff)] == [9]


def test_rows_are_external_rooted_at_gemini_and_never_priced(tmp_path):
    path = _write(tmp_path, [_gemini("msg_1", input_tokens=1000, cached=800)])

    row = rows_for(calls_from_session(path))[0]

    assert row["kind"] == "external"
    assert row["chain"][0] == {"filepath": "gemini", "lineno": 0, "function": "gemini"}
    assert row["chain"][1] == {"filepath": "", "lineno": 0, "function": "reply"}
    assert row["model"] == "gemini-2.5-pro"
    assert row["cost_usd"] == 0.0
    assert row["input_tokens"] == 200


def test_the_project_directory_is_named_after_the_cwd_not_hashed(tmp_path, monkeypatch):
    """The slug is what makes the default root computable from where the run was
    started. A hash would have to be searched for — and a wrong guess here is
    not a wrong number: the run says it found nothing and --root or
    --all-projects is the way past it. The exact slugify is unverified, so this
    asserts the shape (derived, lowercased, stable) and not the spelling."""
    monkeypatch.setattr(gemini, "GEMINI_HOME", tmp_path)

    assert gemini._slug("My Project") == gemini._slug("my project")
    assert "my" in gemini._slug("My Project")
    assert gemini._slug("/") == "project"
    assert gemini._project_dir(Path("/home/dev/My Project")) == tmp_path / "tmp" / "my-project"


def test_a_run_with_no_root_reads_this_project_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gemini, "GEMINI_HOME", tmp_path)
    project = tmp_path / "tmp" / gemini._slug(Path.cwd().name) / "chats"
    project.mkdir(parents=True)
    (project / "session-1.jsonl").write_text(json.dumps(_gemini("msg_1")))

    assert main(["--dry-run"]) == 0

    rows = _rows_from_stdout(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["chain"][0]["filepath"] == "gemini"


def test_all_projects_reads_every_project_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gemini, "GEMINI_HOME", tmp_path)
    for slug in ("one", "two"):
        project = tmp_path / "tmp" / slug / "chats"
        project.mkdir(parents=True)
        (project / "session-1.jsonl").write_text(
            json.dumps(_gemini(f"msg_{slug}", model=f"gemini-2.5-{slug}"))
        )

    assert main(["--all-projects", "--dry-run"]) == 0

    rows = _rows_from_stdout(capsys.readouterr().out)
    assert {row["model"] for row in rows} == {"gemini-2.5-one", "gemini-2.5-two"}
