"""Push Gemini CLI chat sessions into AIght as external-agent spans.

Gemini CLI is an agent the customer runs but did not write, so there is no
customer code on the stack for the SDK's stack-walking attribution to bind
to. Its chain is a step path instead: frame 0 is the agent, frame 1 is the
step (tool + file). It reads Gemini's own session files and instruments
nothing.

    AIGHT_API_KEY=... aight-collect-gemini --dry-run
    AIGHT_API_KEY=... aight-collect-gemini --all-projects --since HEAD~3

    ~/.gemini/tmp/<project>/chats/session-*.jsonl  (~/.aight/gemini_collect.json)

Scoping, resuming, the marker and the watch loop are base's and are described
in the module docstring there. What is Gemini's own:

**The file re-emits whole messages.** There is no on-disk `messages` array:
every time a message changes — a tool result arriving, an answer being
rewritten — the session's record for it is written out again, in full. Read
line by line as new messages, a session with three tool calls in one turn
counts that turn four times. So records are folded by message id with the last
one winning, the same way the Claude Code collector dedupes by `message.id`,
and for the same reason. A record with no id at all is dropped rather than
counted: with nothing to fold on, an update is indistinguishable from a new
call, and a doubled figure is permanent whereas an empty collector is
noticed.

**A rewind rewrites history.** A `$rewindTo <id>` record means the
conversation after that message was abandoned — the user edited an earlier
prompt, or retried — and the records that follow are a fresh branch. Folding
by id alone would keep the abandoned branch's calls, so the fold is truncated
at the id the rewind names. A `$set` record patches bookkeeping (the session's
last-updated stamp) and carries no tokens, so it is skipped rather than read
as a message.

**Tool calls need no join.** They sit on the same record as `tokens` and
`model` — one bill and the activities it paid for, in one object — so each
tool call becomes a step of that call, and its tokens split evenly across
them. File paths are inside the tool's arguments: `file_path` for the file
tools, `command` for `run_shell_command`, and empty for a tool that touches
no file.

**The project directory is named, not hashed.** Gemini names each project's
directory after the *basename of its cwd*, so a cwd can be turned into a path
rather than searched for. The exact slugify is UNVERIFIED (see `_slug`).

Cost is not computed here. The platform prices every row from its own table at
receive time and that number is definitive; rows go up with cost_usd 0.0.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import base
from .base import Call, Collector, _entry_timestamp, _int

AGENT_ID = "gemini"
# UNVERIFIED: this path string is the one thing here taken from earlier
# research rather than from the chat-recording types, so it is named rather
# than inlined. A wrong root is not a wrong number — the run says "nothing to
# collect under ..." and --root or --all-projects is the way past it.
GEMINI_HOME = Path.home() / ".gemini"
PROJECTS_DIRNAME = "tmp"
MARKER_PATH = base.MARKER_DIR / "gemini_collect.json"

# The record types that are not messages. $rewindTo truncates the fold; $set
# patches bookkeeping and carries no tokens of its own. UNVERIFIED: the shapes
# are taken from the names — a rewind names the message to go back to, and a
# $set is a key whose value is the fields it patches. A `$set` that did carry
# messages would be skipped rather than counted, which loses rows instead of
# inventing them.
REWIND_KEY = "$rewindTo"
SET_KEY = "$set"

# Whether Gemini's `input` counts the cached prefix as part of the prompt.
# `input` is `promptTokenCount` and `cached` is `cachedContentTokenCount`, and
# Google's own documentation for promptTokenCount says it is the total
# effective prompt size *including* the cached content — the same convention as
# OpenAI and the opposite of Anthropic's, which is what the proxy reconciles
# for those two. AIght's `input_tokens` is the uncached prompt, so the cached
# count is subtracted.
#
# UNVERIFIED against a real transcript: the chat-recording types name the three
# fields but not their relationship. Getting it wrong is the expensive kind of
# wrong — the cached tokens would be counted in both `input_tokens` and
# `cache_read_tokens` and billed twice, permanently, since cost is frozen at
# receive time. This constant is the one line to flip if a session ever shows
# an `input` that is already exclusive of `cached`.
CACHED_IS_PART_OF_INPUT = True


def _slug(name: str) -> str:
    """Gemini's directory name for a project, from the basename of its cwd:
    lowercased, every run of other characters turned into one dash.

    UNVERIFIED: the exact slugify. It is not a hash, which is the part that
    matters — a hash would have to be searched for, and this can be computed
    from where the run was started. A wrong guess is not a wrong number: the
    default root comes out pointing at nothing, the run says so, and --root or
    --all-projects is the way past it."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


def _project_dir(cwd: Path) -> Path:
    """Where one project's sessions live. The `chats/` level under it is left
    to the scan rather than baked in, so a session file that moves within the
    project directory is still found."""
    return GEMINI_HOME / PROJECTS_DIRNAME / _slug(cwd.name)


def _rewind_to(messages: dict, message_id: str) -> None:
    """Drop every record folded after `message_id` — what a $rewindTo record
    means. An id the fold has never seen leaves it alone: a rewind names a
    message this file has, and one that names nothing is not a reason to
    discard the session."""
    keys = list(messages)
    if message_id not in keys:
        return
    for key in keys[keys.index(message_id) + 1:]:
        del messages[key]


def _steps_of(tool_calls: list) -> list[tuple[str, str]]:
    """(tool, file) per tool call, in the order the record lists them.

    The file is in the call's own arguments, and the two names it goes by are
    asked in turn — `file_path` for the file tools, `command` for the shell.
    A tool that touches no file contributes an empty path, exactly as a shell
    step does in the Claude Code collector.
    """
    steps = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        args = call.get("args")
        args = args if isinstance(args, dict) else {}
        file_path = args.get("file_path") or args.get("command") or ""
        steps.append((str(call.get("name") or "unknown"), str(file_path)))
    return steps or [("reply", "")]


def _call_from_record(entry: dict) -> Call | None:
    """One folded record as a Call, or None when it holds no bill."""
    model = entry.get("model")
    tokens = entry.get("tokens")
    if not model or not isinstance(tokens, dict) or not tokens:
        # Nothing here the platform could price: a user turn, a record with no
        # model named, or a shape this parser does not know.
        return None
    prompt = _int(tokens.get("input"))
    cached = _int(tokens.get("cached"))
    input_tokens = max(prompt - cached, 0) if CACHED_IS_PART_OF_INPUT else prompt
    output_tokens = _int(tokens.get("output"))
    if not (input_tokens or output_tokens or cached):
        return None
    tool_calls = entry.get("toolCalls")
    return Call(
        model=str(model),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cached,
        # Gemini reports cache *reads* only. There is no write count in these
        # records, so a zero here means "not reported", not "nothing cached".
        cache_creation_tokens=0,
        steps=_steps_of(tool_calls if isinstance(tool_calls, list) else []),
        timestamp=_entry_timestamp(entry) or 0.0,
    )


def calls_from_session(path: Path, since: float | None = None) -> list[Call]:
    """Every API call one session file records, folded to one Call per message
    and in the order the messages were first written. `since` drops calls made
    at or before that Unix timestamp, per record rather than per file.

    The fold is the point: a record re-emitted after a tool result replaces the
    copy already held, so the count is messages rather than lines. Folding
    first and filtering second also means the `since` cutoff is applied to the
    message's *latest* timestamp — a turn that was still being written when the
    boundary passed is judged by when it finished, not by the partial record
    that came before it.
    """
    folded: dict[str, dict] = {}
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partially-written trailing line is normal in a live session
        rewind = entry.get(REWIND_KEY)
        if rewind is not None:
            _rewind_to(folded, str(rewind))
            continue
        if SET_KEY in entry:
            continue
        if entry.get("type") != "gemini":
            continue
        message_id = entry.get("id")
        if not message_id:
            # See the module docstring: without the id an update cannot be told
            # from a new call, and counting both would double the session.
            continue
        # Last one wins, and the position of the first copy is kept: dict
        # assignment to an existing key does not move it.
        folded[str(message_id)] = entry

    calls: list[Call] = []
    for entry in folded.values():
        call = _call_from_record(entry)
        if call is None:
            continue
        if since is not None and call.timestamp and call.timestamp <= since:
            # Undated records stay: they can't be shown to predate the cutoff,
            # and dropping calls we can't read is the worse error.
            continue
        calls.append(call)
    return calls


def rows_for(calls: list[Call]) -> list[dict]:
    """Ingest rows for these calls, rooted at Gemini. One row per (step, file,
    model); the bucketing is base's and the only thing this adds is the agent
    id frame 0 carries."""
    return base.rows_for(calls, AGENT_ID)


def _config() -> Collector:
    """This collector, as base's machinery wants it. A function rather than a
    constant so a monkeypatched MARKER_PATH here is picked up by a run."""
    return Collector(
        agent_id=AGENT_ID,
        language=AGENT_ID,
        prog="aight-collect-gemini",
        description="Push Gemini CLI chat sessions into AIght as external-agent spans.",
        root_help="sessions directory to read (default: this project's, under "
                  "~/.gemini/tmp)",
        calls_from=calls_from_session,
        rows_for=rows_for,
        marker_path=MARKER_PATH,
        default_root=lambda: _project_dir(Path.cwd()),
        all_projects_root=lambda: GEMINI_HOME / PROJECTS_DIRNAME,
    )


def main(argv: list[str] | None = None) -> int:
    return base.main(_config(), argv)


def cli() -> None:
    """Console entry point: `aight-collect-gemini`."""
    base.entry_point(main)


if __name__ == "__main__":
    cli()
