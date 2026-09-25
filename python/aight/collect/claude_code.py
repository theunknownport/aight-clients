"""Push Claude Code transcripts into AIght as external-agent spans.

Claude Code is an agent the customer runs but did not write, so there is no
customer code on the stack for the SDK's stack-walking attribution to bind
to. Its chain is a step path instead: frame 0 is the agent, frame 1 is the
step (tool + file). It reads Claude Code's own transcripts; it instruments
nothing, and Claude Code is not asked to know it exists.

    AIGHT_API_KEY=... aight-collect --dry-run
    AIGHT_API_KEY=... aight-collect --since HEAD~3
    AIGHT_API_KEY=... aight-collect --all-projects --all-time

Installed with the SDK, so `pip install aight` is all it takes. Without
installing anything:

    AIGHT_API_KEY=... uvx --from aight aight-collect --dry-run

Pushes accumulate. The API stores calls and cost per (chain, model) and a
second push of the same call adds to them again, so pushing a directory twice
doubles everything the first push counted. Cost is frozen at receive time and
never recomputed, so there is no repair path either — the run has to be
scoped rather than re-run blind. So it resumes per transcript file: a
successful push records the newest call it sent from each file (a small
marker in ~/.aight/), and the next plain run sends only calls newer than
that. Nothing to remember, nothing re-sent — and nothing re-sent for a file
that a different root, or a run from a different directory, turns up too.

    --since <ISO date|git rev>   move that boundary by hand
    --all-time                   every call in these transcripts, however old.
                                 This is the one that re-pushes history.

A transcript file with no entry has nothing to resume from, so its first run
starts at the last 24 hours rather than at the beginning of time. The filter
is per call — each entry's own `timestamp`, not the file's mtime — so a
session file that spans the boundary contributes exactly the calls made after
it.

Three details of the transcript format are easy to get wrong and each
changes the numbers:

- Claude Code writes the same API call to the transcript more than once
  (measured: 94 duplicate rows against 51 calls in one session). Counted
  raw, every figure is 2.2-2.8x too high depending on the session, so
  calls are deduped by message.id.
- One call can emit several tool_use blocks — one bill for several
  activities — so its tokens split evenly across them. Calls with no
  tool_use block become a single "reply" step.
- Usage fields are additive: a call's total input is input_tokens plus
  cache_read_input_tokens plus cache_creation_input_tokens. That is the
  opposite of the OpenAI-shaped convention the proxy reconciles, and it is
  why this parser does no arithmetic on them.

Cost is not computed here. The platform prices every event from its own
table at receive time and that number is definitive; this collector sends
cost_usd 0.0 on every row precisely so it never offers a number of its own.

Everything that is not Claude Code's own format — the resume boundary, the
marker, the scopes, the loop, the push — lives in `base`, because all four
collectors need it identically and a second copy of the boundary arithmetic
is how a fleet gets double-billed. This module is the parser and the config
that points it at Claude Code's directory.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import base
from .base import Call, Collector, _entry_timestamp

AGENT_ID = "claude-code"
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
# The marker is this collector's own — see base.MARKER_DIR for why no two
# collectors share one.
MARKER_PATH = base.MARKER_DIR / "claude_code_collect.json"
# Where the marker lived before it was keyed per file. Only its existence is
# looked at, never its contents: it recorded one boundary per root, which
# cannot be turned into one per file. An error beats silently re-windowing.
LEGACY_MARKER_PATH = Path(".aight/claude_code_collect.json")


def _transcript_dir(cwd: Path) -> Path:
    """Claude Code names each project directory by the cwd with separators
    replaced by dashes."""
    return TRANSCRIPT_ROOT / str(cwd).replace("/", "-")


def _steps_of(message: dict) -> list[tuple[str, str]]:
    steps = []
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_input = block.get("input") or {}
        steps.append((block.get("name") or "unknown", tool_input.get("file_path") or ""))
    return steps or [("reply", "")]


def calls_from_transcript(path: Path, since: float | None = None) -> list[Call]:
    """Every unique API call in one transcript file, in file order. `since`
    drops calls made at or before that Unix timestamp — git's reading of
    "since", and the one that makes a resume exact: the boundary is a call
    this run already pushed, so it must not come round again.

    The cutoff is applied to each entry's own `timestamp` rather than to the
    file (its mtime, or "does it hold anything newer"): a session file can
    span the boundary, and a file-level test would either re-push everything
    in it or drop a live session's newer calls along with its older ones."""
    seen: dict[str, Call] = {}
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partially-written trailing line is normal in a live session
        if entry.get("type") != "assistant":
            continue
        timestamp = _entry_timestamp(entry)
        if since is not None and timestamp is not None and timestamp <= since:
            # Undated entries stay: they can't be shown to predate the cutoff,
            # and dropping calls we can't read is the worse error. ponytail:
            # that also means nothing ever filters them out, so a transcript
            # without timestamps re-pushes on every run — Claude Code writes
            # them on every entry, so this is a shape we have not seen.
            continue
        message = entry.get("message") or {}
        usage = message.get("usage") or {}
        message_id = message.get("id")
        model = message.get("model")
        if not message_id or not model or not usage:
            continue
        if not (
            usage.get("input_tokens")
            or usage.get("output_tokens")
            or usage.get("cache_read_input_tokens")
            or usage.get("cache_creation_input_tokens")
        ):
            # A placeholder entry — real transcripts carry "<synthetic>" ones —
            # with no tokens recorded was never a billed call, and its model is
            # not one the platform can price. Dropped here rather than in
            # rows_for, so it cannot bump a real (tool, file, model) call count.
            continue
        if message_id in seen:
            continue
        seen[message_id] = Call(
            model=model,
            input_tokens=usage.get("input_tokens", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
            cache_read_tokens=usage.get("cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
            steps=_steps_of(message),
            timestamp=timestamp or 0.0,
        )
    return list(seen.values())


def rows_for(calls: list[Call]) -> list[dict]:
    """Ingest rows for these calls, rooted at Claude Code.

    One row per (step, file, model), tokens summed; the bucketing is shared
    with every other collector and lives in base. The only thing this adds is
    the agent id frame 0 carries."""
    return base.rows_for(calls, AGENT_ID)


def _config() -> Collector:
    """This collector, as base's machinery wants it. A function rather than a
    constant so a monkeypatched MARKER_PATH here is picked up by a run — the
    marker path is read at the start of every run, never captured at import."""
    return Collector(
        agent_id=AGENT_ID,
        language=AGENT_ID,
        prog="aight-collect",
        description="Push Claude Code transcripts into AIght as external-agent spans.",
        root_help="transcript directory (default: this project's)",
        calls_from=calls_from_transcript,
        rows_for=rows_for,
        marker_path=MARKER_PATH,
        default_root=lambda: _transcript_dir(Path.cwd()),
        all_projects_root=lambda: TRANSCRIPT_ROOT,
        legacy_marker_path=LEGACY_MARKER_PATH,
    )


def main(argv: list[str] | None = None) -> int:
    return base.main(_config(), argv)


def cli() -> None:
    """Console entry point: `aight-collect`."""
    base.entry_point(main)


if __name__ == "__main__":
    cli()
