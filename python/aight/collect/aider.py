"""Push Aider analytics events into AIght as external-agent spans.

Aider is an agent the customer runs but did not write, so there is no customer
code on the stack for the SDK's stack-walking attribution to bind to. Its
chain is a step path instead: frame 0 is the agent, frame 1 is the step. It
reads Aider's own analytics log and instruments nothing.

    AIGHT_API_KEY=... aight-collect-aider --root ~/analytics/aider.jsonl
    AIDER_ANALYTICS_LOG=~/analytics/aider.jsonl aight-collect-aider --dry-run

    <whatever --analytics-log named>   (~/.aight/aider_collect.json)

Scoping, resuming, the marker and the watch loop are base's and are described
in the module docstring there. What is Aider's own:

**Aider has to be asked to record anything.** Nothing is written unless the
session was started with `--analytics-log FILE` or `AIDER_ANALYTICS_LOG` is
set: Aider has no default analytics path, so there is no directory to look in
and no log to find. A session that already ran without the flag cannot be
backfilled — its spend was never written down, and does not exist anywhere to
collect.

That is why a run with neither `--root` nor the environment variable stops
with a message saying exactly that, instead of collecting zero files and
exiting quietly. "Nothing to collect" is what a working collector says on a
quiet day; the two must not read the same, or the mistake is a user who
believes their Aider spend is being tracked.

**Every line is an event, and only `message_send` is a bill.** One prompt sent
to one model, with the model, the token counts and Aider's own money figures
in `properties`. `time` is integer Unix seconds — the unit the resume boundary
is kept in too, so a call written in the same second as the newest one a run
already sent is skipped by the next run's cutoff. That is a one-second window
inherent to the format; the alternative, re-sending the whole second it might
fall in, doubles whatever it does contain.

Aider appends one line per event and does not rewrite a message as it changes,
so a line is an event and there is no id to fold on — the two traps that need
folding elsewhere (see the Claude Code and Gemini parsers) have no analogue
here.

**Aider reports no cache tokens at all**, so every row goes up with
cache_read_tokens and cache_creation_tokens 0. A zero here means *not
reported*, not *nothing was cached*: a cache-heavy Aider session is
under-reported on the cache axes and nothing in the log distinguishes the two.

**`prompt_tokens` is not one API call's prompt.** Aider accumulates it across
every LLM call a message took — the message goes out, a tool call comes back,
the result goes out again — so the event written at the end carries the sum of
all of them. The tokens are therefore right and the call *count* is not: a
message that took four round trips is one row with `calls` 1.

**`main_model` is Aider's own name for the model**, after whatever redaction
Aider applies before writing it, and it is passed through unchanged. The
platform prices by that exact string, so a redacted or aliased name is a row
it cannot price. An unpriced row is visible in the workspace; a price invented
here would not be.

`cost` and `total_cost` are Aider's money and are deliberately not read: the
platform prices every row at receive time from its own table and that number
is definitive, so cost_usd 0.0 is the only honest row this collector can send.
`total_tokens` is prompt plus completion and is not read either — costing a
sum costs the parts twice. A record with only a total and no split is dropped:
prompt and completion are priced at different rates, and splitting a total by
guess is inventing the split.

Aider records no tool calls and no file paths, so every row is a single
`reply` step. `edit_format` says how the edit was applied and is read past
rather than turned into a step — a step that is not a tool call would read as
one in line review.

The log is a file at a path a person chose rather than a directory of
transcripts, which is why base's `collect` treats a file root as itself.

Cost is not computed here. The platform prices every row from its own table at
receive time and that number is definitive.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import base
from .base import Call, Collector, _int

AGENT_ID = "aider"
ANALYTICS_LOG_ENV = "AIDER_ANALYTICS_LOG"
MARKER_PATH = base.MARKER_DIR / "aider_collect.json"

# What the message_send properties are called. Named rather than inlined
# because every one of them is money: a typo in a key here reads as a call with
# no tokens, which is dropped silently, and the spend is never reported.
MODEL_KEY = "main_model"
PROMPT_KEY = "prompt_tokens"
COMPLETION_KEY = "completion_tokens"

NO_ROOT_MESSAGE = (
    "Aider writes nothing unless it was started with --analytics-log FILE, or\n"
    "AIDER_ANALYTICS_LOG is set. There is no default location to read, and a\n"
    "session that already ran without the flag cannot be collected: Aider\n"
    "recorded nothing for it.\n"
    "Start Aider with --analytics-log FILE, then pass --root FILE or set\n"
    "AIDER_ANALYTICS_LOG to that same path."
)


def _record_time(entry: dict) -> float | None:
    """One event's own `time` as Unix seconds — None if it hasn't got a usable
    one. A number rather than an ISO string, which is why this is Aider's own
    reader and not base's _entry_timestamp: what is in the file is a count of
    seconds, so there is nothing to parse, only to reject. A value in
    milliseconds would read as a time far in the future and re-open every
    file's window on each run, which is the doubling base's boundary exists to
    prevent — hence `float` over an int cast, and hence nothing but a real
    number being accepted."""
    raw = entry.get("time")
    return float(raw) if isinstance(raw, (int, float)) else None


def calls_from_log(path: Path, since: float | None = None) -> list[Call]:
    """Every billed message in one analytics log, in file order. `since` drops
    events at or before that Unix timestamp, per event rather than per file,
    for the reason the shared boundary arithmetic gives in base.pending."""
    calls: list[Call] = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partially-written trailing line is normal in a live session
        if entry.get("event") != "message_send":
            continue
        timestamp = _record_time(entry)
        if since is not None and timestamp is not None and timestamp <= since:
            # Undated events stay: they can't be shown to predate the cutoff,
            # and dropping calls we can't read is the worse error.
            continue
        properties = entry.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        model = properties.get(MODEL_KEY)
        prompt = _int(properties.get(PROMPT_KEY))
        completion = _int(properties.get(COMPLETION_KEY))
        if not model or not (prompt or completion):
            # Nothing the platform could price. A model that never got named,
            # or a record with only a total_tokens — see the module docstring.
            continue
        calls.append(
            Call(
                model=str(model),
                input_tokens=prompt,
                output_tokens=completion,
                # Aider has no cache fields at all; see the module docstring for
                # what this zero does and does not claim.
                cache_read_tokens=0,
                cache_creation_tokens=0,
                steps=[("reply", "")],
                timestamp=timestamp or 0.0,
            )
        )
    return calls


def rows_for(calls: list[Call]) -> list[dict]:
    """Ingest rows for these calls, rooted at Aider. One row per (step, file,
    model); the bucketing is base's and the only thing this adds is the agent
    id frame 0 carries."""
    return base.rows_for(calls, AGENT_ID)


def _analytics_log() -> Path | None:
    """The log AIDER_ANALYTICS_LOG names, or None when nothing points at one.

    None is not an error state here — it is the truth about Aider, which
    records only when it was asked to. base.main prints NO_ROOT_MESSAGE and
    stops, rather than walking a directory that holds nothing."""
    named = os.environ.get(ANALYTICS_LOG_ENV)
    return Path(named) if named else None


def _config() -> Collector:
    """This collector, as base's machinery wants it. A function rather than a
    constant so a monkeypatched MARKER_PATH here is picked up by a run."""
    return Collector(
        agent_id=AGENT_ID,
        language=AGENT_ID,
        prog="aight-collect-aider",
        description="Push Aider analytics events into AIght as external-agent spans.",
        root_help="path to Aider's analytics log: the FILE "
                  "Aider was started with --analytics-log, or AIDER_ANALYTICS_LOG",
        calls_from=calls_from_log,
        rows_for=rows_for,
        marker_path=MARKER_PATH,
        default_root=_analytics_log,
        # One log holds every project Aider ran, so there is nothing for
        # --all-projects to widen to.
        no_root_message=NO_ROOT_MESSAGE,
    )


def main(argv: list[str] | None = None) -> int:
    return base.main(_config(), argv)


def cli() -> None:
    """Console entry point: `aight-collect-aider`."""
    base.entry_point(main)


if __name__ == "__main__":
    cli()
