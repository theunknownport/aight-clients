import json
import time
from datetime import UTC, date, datetime, timedelta

from aight.collect import claude_code as claude_code_collect
import pytest
from aight.collect.claude_code import (
    _split_evenly,
    calls_from_transcript,
    main,
    parse_since,
    rows_for,
)

# Read at import, before any fixture patches it: the marker path the module
# really ships with, which is what tells a run from one directory from a run
# from another.
REAL_MARKER_PATH = claude_code_collect.MARKER_PATH


def _write(tmp_path, entries):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries))
    return path


def _rows_from_stdout(out):
    """The rows --dry-run prints, past the summary line above them."""
    return json.loads(out[out.index("["):])


def _assistant(message_id, content, input_tokens=10, output_tokens=5,
               cache_read=0, cache_creation=0, model="gpt-4o",
               timestamp="2026-09-21T11:00:00Z"):
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "id": message_id,
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


def test_split_evenly_preserves_the_total():
    assert _split_evenly(10, 1) == [10]
    assert _split_evenly(10, 2) == [5, 5]
    # The remainder lands on the first part; 10 split 3 ways must still be 10.
    assert sum(_split_evenly(10, 3)) == 10
    assert _split_evenly(11, 2) == [6, 5]
    # Edges: a zero total, and fewer tokens than parts, stay exact and
    # non-negative rather than going negative to make the parts sum.
    assert _split_evenly(0, 5) == [0, 0, 0, 0, 0]
    assert sum(_split_evenly(2, 5)) == 2
    assert min(_split_evenly(2, 5)) >= 0


def test_calls_from_transcript_dedupes_repeated_message_ids(tmp_path):
    # Claude Code writes the same call more than once; counted raw, every
    # number is 2.2-2.8x too high depending on the session.
    path = _write(tmp_path, [
        _assistant("msg_1", [{"type": "text", "text": "hi"}]),
        _assistant("msg_1", [{"type": "text", "text": "hi"}]),
        _assistant("msg_1", [{"type": "text", "text": "hi"}]),
        _assistant("msg_2", [{"type": "text", "text": "yo"}]),
    ])
    assert len(calls_from_transcript(path)) == 2


def test_calls_from_transcript_skips_partial_json_lines(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps(_assistant("msg_1", [])) + "\n{\"type\": \"assis")
    assert len(calls_from_transcript(path)) == 1


def test_a_call_with_no_tools_becomes_a_reply_step(tmp_path):
    path = _write(tmp_path, [_assistant("msg_1", [{"type": "text", "text": "hi"}])])
    rows = rows_for(calls_from_transcript(path))
    assert len(rows) == 1
    assert rows[0]["chain"][1]["function"] == "reply"
    assert rows[0]["chain"][1]["filepath"] == ""


def test_a_tool_call_carries_the_tool_and_the_file(tmp_path):
    path = _write(tmp_path, [_assistant("msg_1", [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/aight/store.py"}},
    ])])
    rows = rows_for(calls_from_transcript(path))
    assert rows[0]["chain"][1] == {"filepath": "src/aight/store.py", "lineno": 0, "function": "Edit"}


def test_a_call_with_several_tools_splits_its_tokens_between_them(tmp_path):
    path = _write(tmp_path, [_assistant("msg_1", [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    ], input_tokens=10, output_tokens=5)])
    rows = rows_for(calls_from_transcript(path))

    assert len(rows) == 2
    # Split evenly, and — critically — still summing back to the real total.
    assert sum(r["input_tokens"] for r in rows) == 10
    assert sum(r["output_tokens"] for r in rows) == 5
    assert {r["chain"][1]["function"] for r in rows} == {"Edit", "Bash"}
    assert next(r for r in rows if r["chain"][1]["function"] == "Bash")["chain"][1]["filepath"] == ""


def test_rows_are_marked_external_and_rooted_at_the_agent(tmp_path):
    path = _write(tmp_path, [_assistant("msg_1", [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
    ], cache_read=1000)])
    rows = rows_for(calls_from_transcript(path))

    assert rows[0]["kind"] == "external"
    assert rows[0]["chain"][0]["filepath"] == "claude-code"
    assert rows[0]["cache_read_tokens"] == 1000
    assert rows[0]["model"] == "gpt-4o"
    # The money-path invariants: nothing is priced here, and the cache tokens
    # above are NOT folded into input_tokens.
    assert rows[0]["cost_usd"] == 0.0
    assert rows[0]["input_tokens"] == 10


def test_a_call_with_no_tokens_at_all_produces_no_row(tmp_path):
    # Real transcripts carry placeholder entries (model "<synthetic>") with all
    # four token counts zero. They were never billed, and the platform cannot
    # price the model. The rule is general — no tokens recorded — and it runs in
    # calls_from_transcript so a dropped call cannot bump a real bucket's count.
    path = _write(tmp_path, [
        _assistant("msg_1", [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]),
        _assistant("msg_2", [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}],
                   input_tokens=0, output_tokens=0, model="<synthetic>"),
    ])
    calls = calls_from_transcript(path)
    assert [call.model for call in calls] == ["gpt-4o"]

    rows = rows_for(calls)
    assert len(rows) == 1
    assert rows[0]["calls"] == 1


def test_same_tool_and_file_across_calls_accumulate_into_one_row(tmp_path):
    path = _write(tmp_path, [
        _assistant("msg_1", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]),
        _assistant("msg_2", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]),
    ])
    rows = rows_for(calls_from_transcript(path))
    assert len(rows) == 1
    assert rows[0]["calls"] == 2
    assert rows[0]["input_tokens"] == 20


def test_parse_since_reads_an_iso_date_or_a_git_rev():
    assert parse_since("2026-09-21T00:00:00Z") == datetime(2026, 9, 21, tzinfo=UTC).timestamp()
    # A date alone is local midnight, which is what "--since 2026-09-21" means
    # to the person typing it. mktime is that same reading spelled without a
    # naive datetime.
    assert parse_since("2026-09-21") == time.mktime(date(2026, 9, 21).timetuple())
    # Anything git can't resolve is a bad argument, not a silent "no cutoff".
    with pytest.raises(SystemExit):
        parse_since("not-a-date-and-not-a-rev-9f8e7d6c")


def test_since_excludes_calls_made_before_it(tmp_path, capsys):
    _write(tmp_path, [
        _assistant("msg_old", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "old.py"}}],
                   timestamp="2026-09-20T11:00:00Z"),
        # Exactly on the cutoff, and out — "since" is exclusive, like git's.
        # This is the resume's boundary case: the cutoff can be a call an
        # earlier run already pushed, so it must not come round again.
        _assistant("msg_edge", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "edge.py"}}],
                   timestamp="2026-09-21T00:00:00Z"),
        _assistant("msg_new", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "new.py"}}],
                   timestamp="2026-09-21T11:00:00Z"),
    ])

    assert main(["--root", str(tmp_path), "--dry-run", "--since", "2026-09-21T00:00:00Z"]) == 0

    rows = _rows_from_stdout(capsys.readouterr().out)
    assert [r["chain"][1]["filepath"] for r in rows] == ["new.py"]


def test_a_session_spanning_the_cutoff_keeps_its_newer_calls(tmp_path):
    """The filter is per call, not per file: one session file holds both, and
    a file-level test would either drop the newer call with the older one or
    re-push the whole file."""
    path = _write(tmp_path, [
        _assistant("msg_old", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "old.py"}}],
                   timestamp="2026-09-20T11:00:00Z"),
        _assistant("msg_new", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "new.py"}}],
                   timestamp="2026-09-21T11:00:00Z"),
    ])

    calls = calls_from_transcript(path, since=parse_since("2026-09-21T00:00:00Z"))

    assert len(calls) == 1
    assert calls[0].steps == [("Edit", "new.py")]


def test_the_default_run_does_not_repush_history(tmp_path, monkeypatch, capsys):
    """The plain invocation is the bounded one — and it stays bounded on the
    second run, which is the one that matters. A push of the same call adds
    again (ingest sums on conflict) and cost is frozen at receive time, so
    re-pushing history is permanent double-counting. --all-time is the only
    way to ask for it."""
    monkeypatch.setenv("AIGHT_API_KEY", "test-key")
    monkeypatch.setattr(claude_code_collect, "MARKER_PATH", tmp_path / "marker.json")
    pushed: list[list[dict]] = []
    monkeypatch.setattr(claude_code_collect, "push", lambda rows, key, url: pushed.append(rows))

    now = datetime.now(UTC)
    _write(tmp_path, [
        _assistant("msg_old", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "old.py"}}],
                   timestamp=(now - timedelta(days=3)).isoformat()),
        _assistant("msg_new", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "new.py"}}],
                   timestamp=now.isoformat()),
    ])

    # First run: nothing to resume from, so it reaches a day back — the call
    # from three days ago is out, and 24h is only the first run's business.
    assert main(["--root", str(tmp_path)]) == 0
    assert [[r["chain"][1]["filepath"] for r in rows] for rows in pushed] == [["new.py"]]

    # Second run, same command: the marker says new.py was already sent.
    assert main(["--root", str(tmp_path)]) == 0
    assert len(pushed) == 1
    assert "Nothing new to push" in capsys.readouterr().out

    # --all-time is the explicit re-push, and it is the only thing that
    # reaches back past the marker.
    assert main(["--root", str(tmp_path), "--all-time"]) == 0
    assert {r["chain"][1]["filepath"] for r in pushed[1]} == {"old.py", "new.py"}


def _isolated_marker(tmp_path, monkeypatch):
    """Point both marker paths at the test's tmp dir: MARKER_PATH is the
    developer's real ~/.aight marker and LEGACY_MARKER_PATH is cwd-relative, so
    a marker left behind on this machine (or in the repo, by an older build)
    must not decide what a test does."""
    monkeypatch.setattr(claude_code_collect, "MARKER_PATH", tmp_path / "marker.json")
    monkeypatch.setattr(claude_code_collect, "LEGACY_MARKER_PATH", tmp_path / "no-legacy.json")


@pytest.fixture(autouse=True)
def _no_marker_from_this_machine(tmp_path, monkeypatch):
    """For every test in this module, not just the ones that ask for it: the
    collector consults the marker before it reads a transcript, so without this
    whether a test passes depends on what is in the developer's home directory
    and on the directory pytest was run from — the legacy path is relative, and
    the repo's own documented invocation leaves exactly that file there."""
    _isolated_marker(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """For every test in this module: `main()` asks the API which project the
    key writes to before it pushes anything, so without this every test that
    reaches a push makes a live HTTPS request to whatever the ingest URL
    resolves to.

    That is not merely a slow test — it is one whose answer changes when the
    deployment does. These passed only because production predated
    /api/ingest/whoami and answered 404, which the collector reads as "cannot
    check", so the day that endpoint shipped every one of them would have taken
    a 401 and become a `return 1`.

    None is exactly what the collector sees from a server without the endpoint:
    the check is skipped and the run proceeds, which is the path every test
    here that does not ask for something else means to exercise. The two tests
    that do want the check patch `whoami` in their own bodies, which lands
    after this and wins."""
    monkeypatch.setattr(claude_code_collect, "whoami", lambda key, url: None)


def _recording_push(monkeypatch):
    from aight.collect import claude_code as claude_code_collect

    monkeypatch.setenv("AIGHT_API_KEY", "test-key")
    pushed: list[list[dict]] = []
    monkeypatch.setattr(claude_code_collect, "push", lambda rows, key, url: pushed.append(rows))
    return pushed


def _recent(directory):
    """One transcript file there, holding one call made just now — inside the
    24h window a file with no marker entry is read through."""
    directory.mkdir(parents=True, exist_ok=True)
    return _write(directory, [_assistant("msg_1", [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "new.py"}},
    ], timestamp=datetime.now(UTC).isoformat())])


def test_one_file_under_two_roots_is_pushed_once(tmp_path, monkeypatch, capsys):
    """Coverage belongs to the transcript file, not to the root the run
    happened to pass. --root <base> and --root <base>/transcripts find the same
    .jsonl under the same resolved path, and the second run must send nothing:
    a re-push sums on conflict and cost is frozen at receive time, so it would
    be permanently doubled."""
    pushed = _recording_push(monkeypatch)
    _recent(tmp_path / "base" / "transcripts")

    assert main(["--root", str(tmp_path / "base" / "transcripts")]) == 0
    assert [[r["chain"][1]["filepath"] for r in rows] for rows in pushed] == [["new.py"]]

    # The wider root discovers the same file.
    assert main(["--root", str(tmp_path / "base")]) == 0
    assert len(pushed) == 1
    assert "Nothing new to push" in capsys.readouterr().out


def test_a_relative_root_names_the_same_file(tmp_path, monkeypatch, capsys):
    """absolute vs relative is one file, so one entry — otherwise re-running
    the same command from the project directory, spelled differently, doubles
    everything."""
    pushed = _recording_push(monkeypatch)
    _recent(tmp_path / "base" / "transcripts")

    monkeypatch.chdir(tmp_path)
    assert main(["--root", "base/transcripts"]) == 0
    assert len(pushed) == 1

    # Same file, now named absolutely and reached from another directory.
    monkeypatch.chdir(tmp_path / "base")
    assert main(["--root", str(tmp_path / "base" / "transcripts")]) == 0
    assert len(pushed) == 1
    assert "Nothing new to push" in capsys.readouterr().out


def test_another_directory_is_not_another_first_run(tmp_path, monkeypatch, capsys):
    """The marker is not in the cwd: where the tool was run from is not part
    of what has been pushed."""
    # The fixture above points MARKER_PATH into tmp_path, which is absolute
    # whether or not the real one is — so on its own this test cannot fail for
    # a marker the module made cwd-relative, the one thing it is here to rule
    # out. Pin the shipped path.
    assert REAL_MARKER_PATH.is_absolute(), REAL_MARKER_PATH
    pushed = _recording_push(monkeypatch)
    _recent(tmp_path / "base")

    monkeypatch.chdir(tmp_path / "base")
    assert main(["--root", str(tmp_path / "base")]) == 0
    assert len(pushed) == 1

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert main(["--root", str(tmp_path / "base")]) == 0
    assert len(pushed) == 1
    assert "Nothing new to push" in capsys.readouterr().out


def test_the_marker_is_written_whole_and_leaves_no_temp(tmp_path, monkeypatch):
    _recording_push(monkeypatch)
    track = _recent(tmp_path / "base")

    assert main(["--root", str(tmp_path / "base")]) == 0

    marker = tmp_path / "marker.json"
    state = json.loads(marker.read_text())
    assert state["version"] == 2
    assert set(state["files"]) == {str(track.resolve())}
    # The temp file it was written through is gone, not left next to it.
    assert list(tmp_path.glob("*.tmp")) == []


def test_an_unreadable_marker_stops_the_run(tmp_path, monkeypatch):
    """Read as empty, a damaged marker re-opens every file's first-run window
    at once and re-pushes calls that are already recorded — permanently. A
    loud stop costs one deletion instead."""
    _isolated_marker(tmp_path, monkeypatch)
    pushed = _recording_push(monkeypatch)
    _recent(tmp_path / "base")

    marker = tmp_path / "marker.json"
    marker.write_text('{"version": 2, "files": {"/x": 1')  # a crash mid-write
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path / "base")])
    assert pushed == []
    assert marker.read_text() == '{"version": 2, "files": {"/x": 1'


def test_a_dangling_symlink_marker_stops_the_run(tmp_path, monkeypatch):
    """A marker that is a symlink to something gone — a dotfile manager's link,
    or ~/.aight on a volume that isn't mounted — is a marker that is there and
    unreadable, not one that is absent. Followed, it reads as "nothing pushed
    yet" and re-windows every file at 24h, the exact re-push the stop exists to
    prevent."""
    pushed = _recording_push(monkeypatch)
    _recent(tmp_path / "base")
    marker = tmp_path / "marker.json"
    marker.symlink_to(tmp_path / "gone.json")  # target never exists

    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path / "base")])

    assert pushed == []


def test_a_hand_passed_scope_overrides_an_unreadable_marker(tmp_path, monkeypatch):
    """The stop's own advice has to work. --since/--all-time decide what goes
    up by hand, so the marker is only in a plain run's way — and a run that
    could not read it writes nothing back, since replacing it with coverage
    for the files it happened to touch drops every other file's entry, and the
    next plain run reads a missing entry as a first run."""
    pushed = _recording_push(monkeypatch)
    _recent(tmp_path / "base")
    marker = tmp_path / "marker.json"
    marker.write_text('{"version": 2, "files": {"/x": 1')  # a crash mid-write

    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path / "base")])
    assert pushed == []

    assert main(["--root", str(tmp_path / "base"), "--all-time"]) == 0
    assert main(["--root", str(tmp_path / "base"), "--since", "2026-01-01"]) == 0
    assert len(pushed) == 2
    assert marker.read_text() == '{"version": 2, "files": {"/x": 1'


def test_a_dry_run_leaves_the_marker_untouched(tmp_path, monkeypatch):
    """--dry-run is a look, not a push: it must not record coverage it never
    sent, or the real run after it would find its calls already marked and
    skip them."""
    pushed = _recording_push(monkeypatch)
    _recent(tmp_path / "base")
    marker = tmp_path / "marker.json"

    assert main(["--root", str(tmp_path / "base"), "--dry-run"]) == 0
    assert pushed == []
    assert not marker.exists()

    # Nor does it touch a marker that is already there.
    marker.write_text(json.dumps({"version": 2, "files": {"/other": 1}}))
    assert main(["--root", str(tmp_path / "base"), "--dry-run"]) == 0
    assert json.loads(marker.read_text())["files"] == {"/other": 1}


def test_a_pre_per_file_marker_stops_the_run(tmp_path, monkeypatch):
    """The old marker recorded one boundary per root, which cannot be turned
    into one per file — so it is refused rather than silently ignored, since
    ignoring it re-windows every file at 24h."""
    _recording_push(monkeypatch)
    _recent(tmp_path / "base")

    legacy = tmp_path / "old.json"
    legacy.write_text(json.dumps({str(tmp_path / "base"): time.time()}))
    monkeypatch.setattr(claude_code_collect, "MARKER_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(claude_code_collect, "LEGACY_MARKER_PATH", legacy)

    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path / "base")])


def test_a_key_for_another_project_refuses_instead_of_resuming(tmp_path, monkeypatch, capsys):
    """The one mistake the marker cannot catch by itself.

    A marker records that calls were *sent*, never where. So a key minted in a
    different project's Integrate tab resumes this machine's coverage, pushes
    only what is newer than it into the wrong workspace, and leaves the project
    that was meant permanently missing that window — silently, because every
    one of those pushes succeeds. Refusing is the only safe direction.
    """
    pushed = _recording_push(monkeypatch)
    monkeypatch.setattr(
        claude_code_collect, "whoami", lambda key, url: {"id": "proj_other", "name": "Someone else's"}
    )
    (tmp_path / "base").mkdir()
    _recent(tmp_path / "base")
    claude_code_collect.MARKER_PATH.write_text(
        json.dumps({"version": 2, "project": {"id": "proj_wanted", "name": "The project I meant"}, "files": {}})
    )

    assert main(["--root", str(tmp_path / "base")]) == 1
    assert pushed == [], "a mismatched key must not push at all"
    err = capsys.readouterr().err
    assert "The project I meant" in err and "Someone else's" in err


def test_a_matching_key_pushes_and_names_where_it_went(tmp_path, monkeypatch, capsys):
    """The destination is both reported and recorded. Reported, because "Pushed
    N rows" reads the same whether they landed where you meant or in someone
    else's workspace; recorded, because that is what lets the next run notice a
    key change instead of resuming against the wrong one."""
    pushed = _recording_push(monkeypatch)
    monkeypatch.setattr(
        claude_code_collect, "whoami", lambda key, url: {"id": "proj_1", "name": "My project"}
    )
    (tmp_path / "base").mkdir()
    _recent(tmp_path / "base")

    assert main(["--root", str(tmp_path / "base")]) == 0
    out = capsys.readouterr().out
    assert len(pushed) == 1
    assert '"My project"' in out, out
    assert json.loads(claude_code_collect.MARKER_PATH.read_text())["project"] == {
        "id": "proj_1",
        "name": "My project",
    }
