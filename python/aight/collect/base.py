"""The machinery every collector shares: what to send, and how not to send it twice.

Each collector reads a different agent's own on-disk record — Claude Code's
transcripts, Codex's rollouts, Gemini's chat sessions, Aider's analytics log —
and the parsing genuinely differs, because the file shapes have nothing in
common. Everything *around* the parsing does not differ at all. What to read,
where the last run got to, what a hand-passed scope means, how the loop
behaves, which project the key writes to and how a push is made are the same
questions for every agent, and they are the ones where a second copy goes
wrong quietly:

- Pushes accumulate. The API stores calls and cost per (chain, model) and a
  second push of the same call adds to them again, so pushing a directory
  twice doubles everything the first push counted. Cost is frozen at receive
  time and never recomputed, so there is no repair path either — a run has to
  be *scoped* rather than re-run blind. That is why the boundary arithmetic
  below exists in exactly one place.
- A marker is what records how far a run got, per transcript file. Each
  collector needs its own: two agents cover different files, and a shared
  marker would make each collector's files look like first runs to the other —
  and a first run reaches back 24h and re-pushes whatever it finds.

So a collector module is a parser plus a `Collector` saying which files to read
and how to turn them into calls. Everything else lives here.

A collector never sends a cost. The platform prices every row from its own
table at receive time and that number is definitive; rows go up with
cost_usd 0.0 precisely so a collector never offers a number of its own.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__ as SDK_VERSION

DEFAULT_INGEST_URL = "https://api.aight.studio/api/ingest/spans"
# How far back the very first run reaches, before there is anything to resume
# from. A day covers the sessions you might have just finished; everything
# older was either already pushed or is --all-time's business.
DEFAULT_WINDOW_SECONDS = 24 * 60 * 60
# How often --watch re-scans. Short enough that a session's spend shows up while
# you are still in it, long enough that a day of watching is ~2,880 scans and
# not a busy loop. Each scan is a directory walk over files that are open for
# writing anyway, so there is nothing to gain by going faster than a person can
# read the output.
DEFAULT_INTERVAL_SECONDS = 30
MARKER_VERSION = 2
# Where every collector keeps its marker, and one file each rather than one file
# between them: a marker is coverage of a *transcript file*, and two agents cover
# different transcripts entirely, so a shared file would make each collector's
# files look like first runs to the other — and a first run reaches back 24h and
# pushes what it finds, which the API adds to what it already holds.
#
# In the home directory rather than the working one: a marker that lives in the
# cwd re-opens every file's first-run window the moment the tool is run from
# somewhere else.
MARKER_DIR = Path.home() / ".aight"


@dataclass
class Call:
    """One API call, deduped. `steps` is (tool, file_path) per activity the call
    paid for, or [("reply", "")] when it emitted no tools."""

    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    steps: list[tuple[str, str]] = field(default_factory=list)
    timestamp: float = 0.0


@dataclass(frozen=True)
class Collector:
    """Everything that differs between one agent's collector and the next.

    The functions are the two halves of a parser, and they are separate because
    they answer different questions: `calls_from` reads one file's format — how
    a call is identified in it, where its token counts live, and what the call
    paid for — while `rows_for` decides how a call becomes a row. Every
    collector so far shares the second one; the field is here because it is
    where a collector whose rows are shaped differently would say so.

    Frozen because it is written once per module and read for the life of a
    process: a collector that could be edited mid-run is a resume boundary that
    can move under a push.
    """

    # Frame 0 of every row's chain, and the badge the project shows.
    agent_id: str
    language: str
    # The CLI's own furniture.
    prog: str
    description: str
    root_help: str
    # The parser.
    calls_from: Callable[[Path, float | None], list[Call]]
    rows_for: Callable[[list[Call]], list[dict]]
    # Where this collector's coverage is recorded. Never shared with another.
    marker_path: Path
    # The default --root for this run's directory, or None when the agent has no
    # default location and the run must be told where to look.
    default_root: Callable[[], Path | None]
    # What --all-projects widens to, for an agent that keeps one directory per
    # project. None where the default root is already every project, because a
    # flag that means nothing is worse than a flag that is not there.
    all_projects_root: Callable[[], Path] | None = None
    # Only Claude Code has one: a marker that predates the per-file shape.
    legacy_marker_path: Path | None = None
    # Said to a user who ran without --root where there is no default.
    no_root_message: str = "Pass --root: this collector has no default location to read."


def _split_evenly(total: int, parts: int) -> list[int]:
    """Split `total` into `parts` integers that sum back to exactly `total`,
    remainder on the first.

    Even splitting is an approximation — the true cost of a call is not
    separable per tool — but it has to be exact in aggregate, or an agent's
    total stops matching the transcript it came from."""
    base, extra = divmod(total, parts)
    return [base + extra] + [base] * (parts - 1)


def _int(value: object) -> int:
    """A token count from whatever an agent's file holds — 0 for anything that
    is not a number, never a crash mid-parse.

    Every parser needs this and needs it to agree: the counts come out of files
    this code does not control, one agent writes a null where another writes a
    string, and a parser that raised on either would lose the whole file's
    spend over one odd record. The field names differ per agent; what counts as
    a number does not."""
    return int(value) if isinstance(value, (int, float)) else 0


def _entry_timestamp(entry: dict) -> float | None:
    """One record's own timestamp, as Unix seconds — None if it hasn't got one
    this can read. The ISO form is what every JSONL agent here writes."""
    raw = entry.get("timestamp")
    if not raw:
        return None
    try:
        # fromisoformat reads the trailing "Z" itself, from 3.11 on.
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return None


def parse_since(value: str) -> float:
    """A cutoff in Unix seconds from either an ISO date/timestamp
    ("2026-09-21", "2026-09-21T10:30:00Z") or anything git resolves to a
    commit ("HEAD~3", "main", a sha) — the latter is what makes "--since the
    commit I branched from" work."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        pass
    try:
        done = subprocess.run(
            ["git", "log", "-1", "--format=%ct", value],
            capture_output=True, text=True, check=True,
        )
        return float(done.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        raise SystemExit(
            f"--since {value!r} is neither an ISO date/timestamp nor a git rev git can resolve"
        ) from e


def rows_for(calls: list[Call], agent_id: str) -> list[dict]:
    """Ingest rows, one per (step, file, model), tokens summed.

    `calls` counts steps, not API calls: a call that emitted Edit and Bash
    contributed one step to each. Tokens are what cost is derived from, and
    those still sum to the transcript's real total. Every collector buckets
    this the same way — the only thing it needs from the collector is the agent
    the rows are rooted at.
    """
    buckets: dict[tuple[str, str, str], dict] = {}
    for call in calls:
        parts = len(call.steps)
        inputs = _split_evenly(call.input_tokens, parts)
        outputs = _split_evenly(call.output_tokens, parts)
        reads = _split_evenly(call.cache_read_tokens, parts)
        creations = _split_evenly(call.cache_creation_tokens, parts)
        for index, (tool, file_path) in enumerate(call.steps):
            key = (tool, file_path, call.model)
            bucket = buckets.setdefault(
                key,
                {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                },
            )
            bucket["calls"] += 1
            bucket["input_tokens"] += inputs[index]
            bucket["output_tokens"] += outputs[index]
            bucket["cache_read_tokens"] += reads[index]
            bucket["cache_creation_tokens"] += creations[index]

    return [
        {
            "chain": [
                {"filepath": agent_id, "lineno": 0, "function": agent_id},
                {"filepath": file_path, "lineno": 0, "function": tool},
            ],
            "kind": "external",
            "model": model,
            "calls": bucket["calls"],
            "cost_usd": 0.0,
            "input_tokens": bucket["input_tokens"],
            "output_tokens": bucket["output_tokens"],
            "cache_read_tokens": bucket["cache_read_tokens"],
            "cache_creation_tokens": bucket["cache_creation_tokens"],
            "trace_id": "",
        }
        for (tool, file_path, model), bucket in buckets.items()
    ]


def collect(root: Path) -> list[Path]:
    """The files one run reads: every .jsonl under a directory, or the one file
    a --root named.

    A file is a legitimate root because not every agent keeps a directory —
    Aider's analytics log is a path a person chose, and the environment
    variable that names it names a file.
    """
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.jsonl")) if root.is_dir() else []


def _label(project: dict) -> str:
    """A project as something a person recognises, falling back to the id when
    the server could not name it."""
    name = project.get("name")
    return f'"{name}"' if name else project.get("id", "unknown project")


def whoami(api_key: str, url: str) -> dict | None:
    """The project this key writes to, asked of the API before anything is sent.

    None when the deployment predates /api/ingest/whoami — a server older than
    this script is a normal thing to meet, not an error, so the check is skipped
    rather than blocking the push."""
    base = url.rsplit("/", 1)[0]
    request = urllib.request.Request(
        f"{base}/whoami", headers={"Authorization": f"Bearer {api_key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read() or b"{}").get("project")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # endpoint not deployed yet
            return None
        raise  # a 401 is a bad key, and wants to be loud


def _check_destination(config: Collector, api_key: str, url: str) -> tuple[bool, dict | None]:
    """Ask which project this key writes to, and refuse a key that moved.

    Returns (may continue, where the key writes). The second is None both when
    the server is too old to answer and when it answered without naming a
    project, so the caller cannot use it to decide whether to stop — hence the
    separate flag rather than a bare None meaning "carry on".

    An ingest key is otherwise write-only: every read endpoint wants the Auth0
    JWT, so a collector had no way to ask this before pushing. The marker
    records that calls were *sent*, never where, so a key minted in another
    project's Integrate tab would resume this machine's coverage, send only
    what is newer than it into the wrong workspace, and leave the project that
    was meant permanently missing that window — silently, because every one of
    those pushes succeeds.
    """
    try:
        destination = whoami(api_key, url)
    except urllib.error.HTTPError as e:
        print(f"Ingest rejected with {e.code}: {e.read().decode(errors='replace')}",
              file=sys.stderr)
        return False, None
    except urllib.error.URLError as e:
        print(f"Could not reach {url}: {e.reason}", file=sys.stderr)
        return False, None

    recorded = _marker_project(config)
    if destination and recorded and destination["id"] != recorded["id"]:
        print(
            f"This key writes to {_label(destination)}, but {config.marker_path} records "
            f"calls already sent to {_label(recorded)}.\n"
            f"A plain run would skip those calls and push the rest to the wrong "
            f"workspace. Use the key for {_label(recorded)}, or pass --since to "
            f"scope a deliberate re-send.",
            file=sys.stderr,
        )
        return False, None
    return True, destination


def _marker_project(config: Collector) -> dict | None:
    """The project this machine's marker recorded on its last successful push.

    None when the marker predates the field. Read here rather than folded into
    _push_state's return: that function's shape is load-bearing for resumption
    and gains nothing from carrying this."""
    try:
        state = json.loads(config.marker_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    project = state.get("project") if isinstance(state, dict) else None
    return project if isinstance(project, dict) and project.get("id") else None


def push(rows: list[dict], api_key: str, url: str, language: str) -> dict:
    """POST rows to the ingest endpoint.

    `language` names the collector in the badge the project shows, and is a
    parameter rather than a constant because the proxy sends rows too — the
    wire call is identical and only the label differs, so there is one copy of
    it here rather than a second one that drifts. It has no default: every
    caller knows which collector it is, and a wrong badge is a row attributed
    to an agent that did not make the call.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(rows).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-Aight-Sdk-Language": language,
            # Read from the package rather than written here. This was a third
            # copy of the version number, and a header that disagrees with the
            # installed distribution is worse than no header: it makes the
            # project's SDK badge claim a release the rows did not come from.
            "X-Aight-Sdk-Version": SDK_VERSION,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    # The body names the project the rows landed in. Read rather than discarded:
    # a 2xx means "accepted", never "accepted where you meant", and that is the
    # difference between a wrong key being obvious and being silent.
    try:
        return json.loads(body or b"{}")
    except json.JSONDecodeError:
        return {}


def _push_state(config: Collector) -> dict[str, float]:
    """{resolved transcript path: newest call time pushed} — empty when
    nothing has been pushed from this machine yet.

    A marker that exists but doesn't parse as this shape is a hard stop
    rather than an empty one. Read as empty it would re-open every file's
    first-run window at once and push calls that are already recorded; ingest
    sums on conflict and cost is frozen at receive time, so that doubling is
    permanent and only a wiped database undoes it. Failing loudly costs one
    deletion — or one --since/--all-time, which scopes the run by hand and so
    has nothing to lose from it (main catches this stop for those runs)."""
    path = config.marker_path
    legacy = config.legacy_marker_path
    # lexists, not exists: a marker that is a symlink to something gone — a
    # dotfile manager's link, or $HOME/.aight on a volume that isn't mounted —
    # is present and unreadable, not absent. Followed, it reads as "nothing
    # pushed from this machine yet" and re-windows every file at 24h, which is
    # the re-push the stop below exists to prevent.
    if not os.path.lexists(path) and legacy is not None and os.path.lexists(legacy):
        path = legacy  # the pre-per-file marker: same job, old shape
    if not os.path.lexists(path):
        return {}
    try:
        raw = path.read_text()
    except OSError as e:  # incl. a dangling symlink, whose target is what vanished
        raise SystemExit(f"Can't read {path}: {e}") from e
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        state = None
    if (
        not isinstance(state, dict)
        or state.get("version") != MARKER_VERSION
        or not isinstance(state.get("files"), dict)
    ):
        raise SystemExit(
            f"{path} isn't a marker this version understands — it predates "
            f"per-transcript coverage, or it was damaged mid-write. Delete it to "
            f"start a fresh one (which reaches back only "
            f"{DEFAULT_WINDOW_SECONDS // 3600}h), or pass --since/--all-time to "
            f"scope this run by hand. Those are the two ways past this stop, "
            f"which is here so a plain run cannot silently re-push calls that "
            f"are already recorded."
        )
    return {p: t for p, t in state["files"].items() if isinstance(t, (int, float))}


def _remember_covered(
    config: Collector,
    newest_by_file: dict[Path, float],
    state: dict[str, float],
    project: dict | None = None,
) -> None:
    """Record what a successful push covered, per transcript file — the newest
    call sent from each, not the wall clock at push time: a call written to
    the transcript while this run was reading it is newer than anything we
    sent, so the next run picks it up rather than losing it.

    `state` is what _push_state read at the start of this run — passed in
    rather than read again here, so a run merges into the coverage it actually
    resumed from.

    Never moves an entry backwards, so an --all-time run whose transcripts
    hold nothing newer than a previous run covered can't re-open that gap and
    have the next run push it a second time.

    Written to a temp file in the same directory and os.replace'd into place:
    a crash mid-write can't leave a half-written marker, which the next run
    would refuse to read."""
    for path, timestamp in newest_by_file.items():
        key = str(path)
        existing = state.get(key)
        if not isinstance(existing, (int, float)) or timestamp > existing:
            state[key] = timestamp
    config.marker_path.parent.mkdir(parents=True, exist_ok=True)
    temp = config.marker_path.with_suffix(".tmp")  # same directory, so replace is atomic
    # `project` is the destination the server named for this push, recorded
    # beside the coverage because the coverage alone is ambiguous: it says these
    # calls were *sent*, never where. A later run holding a key for a different
    # project can only notice by comparing against this. Kept when a run cannot
    # resolve one — a server too old to answer must not erase what a newer push
    # recorded. The version stays at 2: bumping it would make every existing
    # marker unreadable, and the stop that guards that tells the reader to
    # delete it, which re-windows every file at 24h and is itself a re-push.
    payload: dict = {"version": MARKER_VERSION, "files": state}
    destination = project or _marker_project(config)
    if destination:
        payload["project"] = destination
    temp.write_text(json.dumps(payload))
    os.replace(temp, config.marker_path)


def pending(
    config: Collector,
    paths: list[Path],
    covered: dict[str, float] | None,
    manual: bool,
    since: float | None,
) -> tuple[list[Call], dict[Path, float], list[Path]]:
    """What has not been pushed yet, across `paths`.

    Returns the calls, the newest call time per file — which is the next run's
    resume boundary — and the files that had nothing to resume from. The caller
    reports the last of those, because "why is only the last day going up" is
    worth saying and worth saying in different words to a one-shot run than to
    a loop that will say it again in thirty seconds.

    Each transcript is measured on its own: one file, one boundary, whichever
    root found it and wherever the tool was run from. This is the part a watch
    loop must not reimplement — the boundary arithmetic is what stands between
    a re-run and permanently doubled spend, so there is one copy of it.
    """
    window = time.time() - DEFAULT_WINDOW_SECONDS
    calls: list[Call] = []
    newest_by_file: dict[Path, float] = {}
    fresh: list[Path] = []
    for path in paths:
        key = path.resolve()
        if manual:
            file_since = since
        # `covered is not None` rather than a bare .get: a run that passed
        # --since against an unreadable marker carries None here, and would
        # otherwise fail on the attribute rather than falling through to the
        # scope it was given.
        elif covered is not None and (entry := covered.get(str(key))) is not None:
            file_since = entry
        else:
            # Nothing to resume this file from, so it starts at a day rather
            # than at the beginning of time: pushes add up, and a full-history
            # re-push doubles every figure already recorded for it.
            file_since = window
            fresh.append(path)
        file_calls = config.calls_from(path, since=file_since)
        calls += file_calls
        # A run of nothing but undated calls has no boundary to record, and
        # recording 0.0 would let the next run re-push the file.
        newest = max((call.timestamp for call in file_calls), default=0.0)
        if newest:
            newest_by_file[key] = newest
    return calls, newest_by_file, fresh


def watch(
    config: Collector,
    root: Path,
    api_key: str,
    url: str,
    *,
    covered: dict[str, float] | None,
    destination: dict | None,
    interval: float,
) -> int:
    """Collect on a loop, so spend is reported while the agent is still running.

    One-shot collection only helps if you remember to run it. This is the
    version you leave running: it re-scans `root` every `interval` seconds and
    pushes whatever appeared since the last tick. New transcript files are
    picked up as they are created, because the scan itself is repeated.

    Two things are deliberately resolved once, by the caller, rather than per
    tick. The marker, because re-reading a file this loop just wrote buys
    nothing; and the key's destination, because a round trip every thirty
    seconds is 2,880 requests a day to answer a question whose answer cannot
    change while the process runs.

    Every tick is a plain (resuming) collection — never a --since or --all-time
    one. Those are one-shot scopes, and applying one on a loop would re-send the
    same history every interval, which the API adds to what it holds. That is
    why main() refuses to combine them with --watch rather than leaving it to
    whoever reads the help text.

    A tick that fails does not end the loop. A dropped connection, a 500, a rate
    limit — the calls are still in the transcript and the next tick sends them,
    so stopping would be the only real mistake. A rejection that will not fix
    itself does end it, because repeating a rejected key every thirty seconds is
    noise rather than resilience.
    """
    while True:
        try:
            calls, newest_by_file, fresh = pending(config, collect(root), covered, False, None)
            if fresh:
                print(f"{len(fresh)} transcript(s) with nothing pushed yet, so only calls "
                      f"from the last {DEFAULT_WINDOW_SECONDS // 3600}h go up. "
                      f"Pass --all-time once to catch them up.")
            rows = config.rows_for(calls)
            if rows:
                result = push(rows, api_key, url, config.language) or {}
                if covered is not None:
                    _remember_covered(
                        config, newest_by_file, covered, destination or result.get("project")
                    )
                landed = result.get("project") or destination
                stamp = datetime.now(UTC).strftime("%H:%M:%S")
                print(f"[{stamp}] Pushed {len(rows)} rows to {_label(landed)}"
                      if landed else f"[{stamp}] Pushed {len(rows)} rows")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            # 429 and 5xx are the server asking for patience; everything else in
            # the 4xx range is this client being wrong, and no amount of waiting
            # fixes a key that was revoked or a project that was deleted.
            if e.code != 429 and e.code < 500:
                print(f"Ingest rejected with {e.code}: {body}", file=sys.stderr)
                return 1
            print(f"Ingest failed with {e.code}: {body} — retrying in {interval:.0f}s",
                  file=sys.stderr)
        except urllib.error.URLError as e:
            print(f"Could not reach {url}: {e.reason} — retrying in {interval:.0f}s",
                  file=sys.stderr)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def main(config: Collector, argv: list[str] | None = None) -> int:
    # Not description=__doc__: a module docstring is fifty lines of rationale
    # for whoever maintains the parser, and `--help` is the first thing a
    # stranger types. The config's description is the one-liner they want; the
    # flags below carry the detail.
    parser = argparse.ArgumentParser(prog=config.prog, description=config.description)
    parser.add_argument("--root", type=Path, default=None, help=config.root_help)
    # Only where it means something: an agent whose default root is already
    # every project it has would make --all-projects a flag that does nothing.
    if config.all_projects_root is not None:
        parser.add_argument("--all-projects", action="store_true",
                            help=f"every project under {config.all_projects_root()}, "
                                 f"not just this one")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--since", default=None,
                       help="only calls made after this time: an ISO date/timestamp "
                            "(2026-09-21, 2026-09-21T10:30:00Z) or a git rev (HEAD~3, a sha)")
    scope.add_argument("--all-time", action="store_true",
                       help="every call in these transcripts, however old — re-pushes history, "
                            "so running it again over an already-pushed directory doubles it")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the rows instead of pushing them")
    parser.add_argument("--watch", action="store_true",
                        help="keep running: re-scan and push on an interval, until interrupted")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS,
                        metavar="SECONDS",
                        help=f"seconds between scans with --watch "
                             f"(default: {DEFAULT_INTERVAL_SECONDS})")
    args = parser.parse_args(argv)

    # --since/--all-time scope every file by hand; a plain run resumes.
    manual = args.all_time or bool(args.since)

    # Both guards protect the same thing, and both sit before any I/O: a
    # combination that cannot be honoured should be refused on the command line,
    # not after a directory walk has already run. The API *adds* what it
    # receives, so a scope that re-sends is fine once and ruinous on a loop —
    # --all-time every thirty seconds would re-send the entire history every
    # tick, and unlike a mistaken one-shot run there is nothing to notice and
    # stop. It would keep doubling until someone read the workspace.
    if args.watch and manual:
        parser.error("--watch cannot be combined with --since or --all-time: those "
                     "name a scope to re-send, and a loop would re-send it every "
                     "interval. Run them once to catch up, then start the watcher.")
    if args.watch and args.dry_run:
        parser.error("--watch with --dry-run would print the same rows forever. "
                     "Pick one.")

    root = args.root
    if root is None:
        if config.all_projects_root is not None and getattr(args, "all_projects", False):
            root = config.all_projects_root()
        else:
            root = config.default_root()
    if root is None:
        # An agent that was never told where to write its record has nothing on
        # disk to find, so there is no default to fall back to and the run has
        # to say why rather than quietly collect nothing.
        print(config.no_root_message, file=sys.stderr)
        return 1
    paths = collect(root)
    # A watch run may legitimately start before there is anything to watch —
    # that is what starting it early means, and the loop re-scans anyway. A
    # --root that is not a directory at all is a different thing: a typo, and a
    # loop over nothing would hide it instead of reporting it.
    if not paths and not (args.watch and root.is_dir()):
        print(f"Nothing to collect under {root}", file=sys.stderr)
        return 1

    if args.all_time:
        since = None
    elif args.since:
        since = parse_since(args.since)
    else:
        since = None  # decided per file below

    # Each transcript is measured on its own: one file, one boundary, whichever
    # root found it and wherever the tool was run from.
    #
    # A hand-passed scope is what decides what goes up, so an unreadable marker
    # can't block it — the stop it raises is for the plain run, which is the
    # one that would silently re-window. But a marker we couldn't read is also
    # one we can't merge into, so that run leaves it alone: rewriting it with
    # just the files it touched would drop every other file's entry, and the
    # next plain run reads a missing entry as a first run and pushes a day of
    # that file again (see _push_state).
    try:
        covered = _push_state(config)
    except SystemExit as e:
        if not manual:
            raise
        print(f"{e}\nUsing the scope you passed instead; that marker is left "
              f"untouched. Delete it when you want a plain run to resume.",
              file=sys.stderr)
        covered = None

    # A watch run branches here, before the scan below, so it does not pay for a
    # collection nobody reads and then immediately repeat it. It resolves its own
    # key and destination, once, and hands them to the loop.
    #
    # These six lines are duplicated from the one-shot path rather than shared
    # with it, and the reason is that path's "nothing new to push" exit: it has
    # to be reachable *without* touching the network, because a quiet machine
    # with no key exits 0 today. Hoisting this above that check to save the
    # duplication would turn a no-op run into a failure.
    if args.watch:
        api_key = os.environ.get("AIGHT_API_KEY")
        if not api_key:
            print("Set AIGHT_API_KEY (Integrate tab of your AIght Workspace)", file=sys.stderr)
            return 1
        url = os.environ.get("AIGHT_INGEST_URL", DEFAULT_INGEST_URL)
        ok, destination = _check_destination(config, api_key, url)
        if not ok:
            return 1
        print(f"Watching {root} every {args.interval:.0f}s — Ctrl-C to stop.")
        return watch(
            config, root, api_key, url,
            covered=covered,
            # The marker's own record is the fallback for a server too old to
            # answer /whoami: it is where the last push from this machine went,
            # which is the best available answer to where the next one should.
            destination=destination or _marker_project(config),
            interval=args.interval,
        )

    window = time.time() - DEFAULT_WINDOW_SECONDS
    calls, newest_by_file, fresh = pending(config, paths, covered, manual, since)

    if fresh:
        print(f"{len(fresh)} transcript(s) with nothing pushed yet, so only calls "
              f"from the last {DEFAULT_WINDOW_SECONDS // 3600}h (since "
              f"{datetime.fromtimestamp(window, UTC):%Y-%m-%d %H:%M}Z) go up. "
              f"Pass --all-time for every call.")

    rows = config.rows_for(calls)
    print(f"{len(paths)} transcript(s), {len(calls)} unique calls, {len(rows)} rows")

    if args.dry_run:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        # Already covered: a push of nothing would still be a round trip, and
        # the marker already says where each of these files is up to.
        print("Nothing new to push")
        return 0

    api_key = os.environ.get("AIGHT_API_KEY")
    if not api_key:
        print("Set AIGHT_API_KEY (Integrate tab of your AIght Workspace)", file=sys.stderr)
        return 1
    url = os.environ.get("AIGHT_INGEST_URL", DEFAULT_INGEST_URL)

    # Which project this key writes to, asked before anything is written.
    ok, destination = _check_destination(config, api_key, url)
    if not ok:
        return 1

    try:
        # `or {}`: a push that returns nothing — an older build, or a stub —
        # degrades to "destination unknown" rather than raising an unrelated
        # AttributeError after the rows have already been accepted server-side.
        result = push(rows, api_key, url, config.language) or {}
    except urllib.error.HTTPError as e:
        # The server's reason (a bad key, the kind-conflict rule) is in the body.
        print(f"Ingest rejected with {e.code}: {e.read().decode(errors='replace')}",
              file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Could not reach {url}: {e.reason}", file=sys.stderr)
        return 1
    if newest_by_file and covered is not None:
        _remember_covered(config, newest_by_file, covered, destination or result.get("project"))
    # Naming the destination is the whole point: "Pushed 5 rows" reads the same
    # whether they landed where you meant or in someone else's workspace.
    landed = result.get("project") or destination
    print(f"Pushed {len(rows)} rows to {_label(landed)}" if landed else f"Pushed {len(rows)} rows")
    return 0


def entry_point(run: Callable[[], int]) -> None:
    """Console entry point helper: `aight-collect` and its siblings.

    The BrokenPipe handshake lives here rather than inline under `__main__`
    because that block is unreachable once the package is installed — the
    entry point calls main() directly, so `aight-collect --dry-run | head`
    would otherwise die on the closed pipe with a traceback instead of
    exiting quietly. `head` closing early is normal for a command whose output
    is meant to be read, not an error to report."""
    try:
        raise SystemExit(run())
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0) from None
