"""Push Codex CLI rollouts into AIght as external-agent spans.

Codex CLI is an agent the customer runs but did not write, so there is no
customer code on the stack for the SDK's stack-walking attribution to bind
to. Its chain is a step path instead: frame 0 is the agent, frame 1 is the
step. It reads Codex's own rollout files and instruments nothing.

    AIGHT_API_KEY=... aight-collect-codex --dry-run
    AIGHT_API_KEY=... aight-collect-codex --since HEAD~3

    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl   (~/.aight/codex_collect.json)
    CODEX_HOME                                     moves the root

Scoping, resuming, the marker and the watch loop are base's and are described
in the module docstring there. What is Codex's own:

**The prompt is not the whole prompt.** `cached_input_tokens` is a *subset* of
`input_tokens` — the cached prefix is already counted inside it — and AIght's
`input_tokens` is the uncached prompt, so the cached count is subtracted here.
That is the opposite convention to Claude Code's, where the cache fields are
additive, and it is the same one the proxy reconciles for OpenAI. Sending the
inclusive number bills the cached tokens twice, permanently: ingest adds what
it receives and cost is frozen at receive time.

**Token usage arrives in two envelopes.** The original is an `event_msg` whose
payload is a `token_count` event, carrying `info.last_token_usage` — the
per-response figure. `info.total_token_usage` sits right beside it and is
cumulative for the session; reading that one instead multiplies the spend by
the number of turns taken so far. The newer one (v0.153+) is a top-level
`token_usage_record` whose payload carries `usage` and the `response_id` it
belongs to — one completed response per record, and it is read in preference
to the older envelope when a file has any (see NEWEST_USAGE_ENVELOPE_WINS).

**The model is not where you would look.** `session_meta` carries the session
id, the cwd, the provider and the git state, and no model at all — the model
lives on `turn_context`, which Codex emits per turn, so the latest one seen is
carried forward to the usage records that follow it. `session_meta` is also
repeated once per resume, so a second one is the same session continuing, not
a new session.

**There is no link between a tool call and the API call that paid for it.**
`function_call`, `custom_tool_call` and `local_shell_call` all carry a
`call_id`; usage records carry a `response_id`; the two are different id
spaces and nothing joins them. File paths *are* recoverable — `apply_patch`
carries absolute paths in its input, `exec_command` carries a workdir — but
attaching them would mean ordering tool calls against usage records within a
turn, which is a guess dressed as a parse, and a wrong one charges one file
for another file's tokens. So every row here is a single `reply` step: the
model and the tokens are exact and the step is the session. That is a real
loss of resolution against the Claude Code collector and it is the honest one
until a shared id exists.

**Unknown types are normal.** `type` is snake_case and the enum has grown four
variants across v0.150 → v0.157, so anything unrecognised is skipped rather
than treated as an error.

Cost is not computed here. The platform prices every row from its own table at
receive time and that number is definitive; rows go up with cost_usd 0.0.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import base
from .base import Call, Collector, _entry_timestamp, _int

AGENT_ID = "codex"
CODEX_HOME_ENV = "CODEX_HOME"
DEFAULT_CODEX_HOME = Path.home() / ".codex"
SESSIONS_DIRNAME = "sessions"
MARKER_PATH = base.MARKER_DIR / "codex_collect.json"

# Whether a rollout can carry both usage envelopes for the same calls. If it
# can, they describe the same API calls — the newer one replacing the older —
# and reading both would double every call in the file, permanently, because
# ingest adds what it receives and cost is frozen at receive time. So the newer
# envelope wins for the whole file, and the older one is read only when there
# is no newer one to read.
#
# UNVERIFIED: no v0.153+ rollout has been read here. The rule is right if the
# envelopes overlap (one superseding the other, which is what the version
# ranges suggest) and wrong if the newer one covers only some calls, in which
# case this skips the older envelope's. Missing rows are the least bad of the
# two mistakes: a parser fix and an --all-time run can put them back, and
# nothing can take a doubled figure out again.
NEWEST_USAGE_ENVELOPE_WINS = True

# UNVERIFIED: whether `cache_write_input_tokens` is part of `input_tokens` too.
# The cached field is a documented subset of the input, and this one is named
# and shaped like its sibling — two details of one prompt rather than tokens
# billed beside it — so it is subtracted with it, keeping
# input + cache_read + cache_creation equal to what the provider counted. If it
# turns out to be additive, the Anthropic convention, this is the line to
# change; the symptom would be a prompt under-reported by its own cache writes.
CACHE_WRITE_IS_PART_OF_INPUT = True


def _uncached_prompt(usage: dict) -> int:
    """The part of the prompt that was not served from cache — the number
    AIght's `input_tokens` wants.

    The subtraction is the whole point of this function, and the reason is in
    the module docstring: Codex's `input_tokens` counts the cached prefix as
    part of the prompt and AIght's does not. `max(..., 0)` rather than a bare
    subtraction, because a provider reporting more cached than input would
    otherwise send a negative count and nothing downstream prices one.
    """
    total = _int(usage.get("input_tokens"))
    cached = _int(usage.get("cached_input_tokens"))
    written = _int(usage.get("cache_write_input_tokens")) if CACHE_WRITE_IS_PART_OF_INPUT else 0
    return max(total - cached - written, 0)


def _usage_from_codex(usage: dict) -> dict | None:
    """AIght's four token counts from one Codex `TokenUsage`, or None when the
    record carries nothing to count.

    `reasoning_output_tokens` is not added to `output_tokens`: OpenAI's usage
    reports reasoning as a *detail* of the output count, so adding it would
    inflate every reasoning-heavy call. `total_tokens` is not read either — it
    is the sum of these, and costing a sum is costing the parts twice.
    """
    if not usage:
        return None
    counts = {
        "input_tokens": _uncached_prompt(usage),
        "output_tokens": _int(usage.get("output_tokens")),
        "cache_read_tokens": _int(usage.get("cached_input_tokens")),
        "cache_creation_tokens": (
            _int(usage.get("cache_write_input_tokens")) if CACHE_WRITE_IS_PART_OF_INPUT else 0
        ),
    }
    if not any(counts.values()):
        # No tokens recorded was never a billed call. Dropped rather than sent
        # as zeros, which would be a claim that the call was free.
        return None
    return counts


def calls_from_rollout(path: Path, since: float | None = None) -> list[Call]:
    """Every API call one rollout file records. `since` drops calls made at or
    before that Unix timestamp, per record rather than per file, for the reason
    the shared boundary arithmetic gives in base.pending.

    The cutoff is applied *after* the two envelopes are reconciled, not while
    the file is read: whether a file uses the newer envelope is a property of
    the file, and letting the cutoff decide it would mean two runs scoping the
    same file by the same clock could read it through different parsers.
    """
    model = ""  # the latest turn_context; Codex names the model once per turn
    counted: dict[str, tuple[float, str, dict]] = {}  # response_id -> record
    fallback: list[tuple[float, str, dict]] = []

    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partially-written trailing line is normal in a live session
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = entry.get("type")
        if kind == "turn_context":
            # The model is here and nowhere else, and one turn_context can name
            # it for many usage records after it.
            model = str(payload.get("model") or model)
            continue
        if kind == "token_usage_record":
            usage = payload.get("usage")
            if isinstance(usage, dict) and usage:
                response_id = payload.get("response_id")
                # An id-less record gets a key of its own rather than a shared
                # placeholder: two of them are two API calls, and one key would
                # collapse them into one. Nothing can fold a re-emission of one
                # — that is what the id is for — which is why this is the
                # fallback and not the rule.
                key = str(response_id) if response_id else f"no-response-id-{len(counted)}"
                counted[key] = (_entry_timestamp(entry) or 0.0, model, usage)
            continue
        if kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            usage = info.get("last_token_usage") if isinstance(info, dict) else None
            # last_token_usage, never total_token_usage: the latter is
            # cumulative for the session and would multiply every turn's spend.
            if isinstance(usage, dict) and usage:
                fallback.append((_entry_timestamp(entry) or 0.0, model, usage))
            continue
        # Everything else — response items, tool calls, unknown types — is not
        # a bill, and the enum grows without notice.

    newest = list(counted.values())
    if NEWEST_USAGE_ENVELOPE_WINS:
        records = newest or fallback
    else:
        records = fallback + newest

    calls: list[Call] = []
    for timestamp, record_model, usage in records:
        if since is not None and timestamp and timestamp <= since:
            continue
        if not record_model:
            # No model has been named yet — a usage record before the first
            # turn_context, or a file that never wrote one. Its rows could not
            # be priced, so there is nothing to send.
            continue
        counts = _usage_from_codex(usage)
        if counts is None:
            continue
        calls.append(
            Call(
                model=record_model,
                steps=[("reply", "")],
                timestamp=timestamp,
                **counts,
            )
        )
    return calls


def rows_for(calls: list[Call]) -> list[dict]:
    """Ingest rows for these calls, rooted at Codex. One row per (step, file,
    model); the bucketing is base's and the only thing this adds is the
    agent id frame 0 carries."""
    return base.rows_for(calls, AGENT_ID)


def _sessions_root() -> Path:
    """Where Codex writes its rollouts, under whatever home CODEX_HOME names."""
    return Path(os.environ.get(CODEX_HOME_ENV) or DEFAULT_CODEX_HOME) / SESSIONS_DIRNAME


def _config() -> Collector:
    """This collector, as base's machinery wants it. A function rather than a
    constant so a monkeypatched MARKER_PATH here is picked up by a run."""
    return Collector(
        agent_id=AGENT_ID,
        language=AGENT_ID,
        prog="aight-collect-codex",
        description="Push Codex CLI rollouts into AIght as external-agent spans.",
        root_help="sessions directory to read (default: $CODEX_HOME/sessions, "
                  "or ~/.codex/sessions)",
        calls_from=calls_from_rollout,
        rows_for=rows_for,
        marker_path=MARKER_PATH,
        # Codex files every session under the one root, so the default is
        # already every project and there is nothing for --all-projects to do.
        default_root=_sessions_root,
    )


def main(argv: list[str] | None = None) -> int:
    return base.main(_config(), argv)


def cli() -> None:
    """Console entry point: `aight-collect-codex`."""
    base.entry_point(main)


if __name__ == "__main__":
    cli()
