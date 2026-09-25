"""Push locally-traced spend data to your hosted AIght Workspace.

Stdlib only (urllib) — reporting cost data home shouldn't require pulling in
`requests` on top of the tracing deps. Get an API key from the "Integrate"
tab of your AIght Workspace, then after a traced run:

    from aight.remote import push
    from aight.tracing import COST_PROCESSOR
    push(COST_PROCESSOR.buckets)  # reads AIGHT_API_KEY from the environment
"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from . import __version__
from .tracing import SNIPPETS, current_trace_id

DEFAULT_INGEST_URL = "https://api.aight.studio/api/ingest/spans"


def _send(request: urllib.request.Request) -> bytes:
    """POST and return the response body, keeping the server's own explanation
    when it fails.

    urllib raises `HTTPError: HTTP Error 401: Unauthorized` — every fact except
    the useful one. The ingest endpoints answer 401/400/402 with a
    `{"detail": ...}` body saying *why*, and that body was being discarded:
    "Unknown or revoked API key" is the difference between re-minting a key in
    five seconds and an afternoon of reading container logs. Losing it was our
    silent failure, not urllib's.

    Raises RuntimeError rather than letting the HTTPError through so the detail
    travels in the message. Nothing catches HTTPError by type — the shakedown
    reports `{type(exc).__name__}: {exc}` and so prints it either way.
    """
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", "")
        except (ValueError, AttributeError):
            detail = ""
        raise RuntimeError(
            f"push to {request.full_url} failed: HTTP {exc.code} {exc.reason}"
            + (f" — {detail}" if detail else "")
        ) from exc


def _frame_dict(filepath: str, lineno: int, function: str) -> dict:
    frame = {"filepath": filepath, "lineno": lineno, "function": function}
    snippet = SNIPPETS.get((filepath, lineno))
    if snippet is not None:
        frame["snippet_start"], frame["snippet"] = snippet
    return frame


def push(buckets, api_key: str | None = None, url: str | None = None) -> None:
    """Push a CostByLineProcessor.buckets dict. Keys are ``(chain, model)``
    pairs, so a line that calls two models contributes two rows, each carrying
    its own model and token counts rather than one blended model name. Rows
    carry no ``cost_usd``: pricing is the platform's, recomputed server-side
    from the model and the four token counts."""
    api_key = api_key or os.environ.get("AIGHT_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No API key. Pass api_key=..., or set AIGHT_API_KEY (get one from "
            "the Integrate tab of your aight.studio dashboard)."
        )
    url = url or os.environ.get("AIGHT_INGEST_URL", DEFAULT_INGEST_URL)
    rows = [
        {
            "chain": [_frame_dict(fp, ln, fn) for fp, ln, fn in chain],
            "calls": b.calls,
            "model": model,
            "input_tokens": b.input_tokens,
            "output_tokens": b.output_tokens,
            # Sent as their own counts, never folded into input_tokens: the
            # platform sums four rates and freezes the result at receive, so an
            # inclusive input count would bill the cached tokens twice — at the
            # full input rate and again at the cache rate — permanently.
            "cache_read_tokens": b.cache_read_tokens,
            "cache_creation_tokens": b.cache_creation_tokens,
            "latency_ms": b.latency_ms,
            "trace_id": b.trace_id,
        }
        for (chain, model), b in buckets.items()
    ]
    if not rows:
        return
    body = json.dumps(rows).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-Aight-Sdk-Language": "python",
            "X-Aight-Sdk-Version": __version__,
        },
    )
    _send(request)
    # Only after the server has acknowledged. The ingest endpoint *adds* each
    # row to what it already holds (see store.ingest_chain_rows), so re-sending
    # these same running totals on the next push would count every call a
    # second time and quietly inflate the AIght Workspace's spend. Clearing on
    # success makes a second push send only what happened since the first.
    # ponytail: no idempotency key, so a push that reaches the server but whose
    # response is lost still double-counts on retry. Add a client run-id if
    # that ever bites.
    buckets.clear()


def push_event(
    event_name: str,
    value: float = 0.0,
    currency: str = "USD",
    event_id: str | None = None,
    trace_id: str | None = None,
    timestamp: float | None = None,
    api_key: str | None = None,
    url: str | None = None,
) -> dict:
    """Report a business KPI/event (a Stripe checkout, a signup, a support
    ticket closed, ...) to power the AIght Workspace's fleet view and hybrid
    ROI attribution (see aight.matching.evaluate_event_match on the backend).

    trace_id defaults to the active TraceContext's trace_id if you're
    inside a `with TraceContext(...):` block — that gives the backend an
    EXPLICIT match instead of falling back to its time-window heuristic.

    Returns the ingest endpoint's JSON response, shaped
    {"ingested": n, "results": [...]} — each entry of "results" carries the
    match_type ("EXPLICIT"/"IMPLICIT"/"UNMATCHED") it resolved to, plus its
    confidence_score. It is not the match result itself."""
    api_key = api_key or os.environ.get("AIGHT_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No API key. Pass api_key=..., or set AIGHT_API_KEY (get one from "
            "the Integrate tab of your aight.studio dashboard)."
        )
    url = url or os.environ.get("AIGHT_EVENTS_INGEST_URL", DEFAULT_INGEST_URL.replace("/spans", "/events"))
    event = {
        "event_id": event_id or str(uuid.uuid4()),
        "event_name": event_name,
        "value": value,
        "currency": currency,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "trace_id": trace_id or current_trace_id() or "",
    }
    body = json.dumps(event).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    return json.loads(_send(request))


def push_value(value_by_file, api_key: str | None = None, url: str | None = None) -> None:
    """Push aight.tracing.VALUE_BY_FILE to /api/ingest/value. Same auth and
    shape as push(), just a different endpoint and row shape — including the
    clear-on-success, for the same reason: report_value() only ever *adds* to
    the accumulator it hands you, so a second push of the same running totals
    would insert the same earnings again. /api/ingest/value is a plain INSERT
    with no dedup, so that inflation would be permanent."""
    api_key = api_key or os.environ.get("AIGHT_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No API key. Pass api_key=..., or set AIGHT_API_KEY (get one from "
            "the Integrate tab of your aight.studio dashboard)."
        )
    url = url or os.environ.get("AIGHT_VALUE_INGEST_URL", DEFAULT_INGEST_URL.replace("/spans", "/value"))
    rows = [{"filepath": filepath, "value_usd": value} for filepath, value in value_by_file.items()]
    if not rows:
        return
    body = json.dumps(rows).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    _send(request)
    # Only after the server has acknowledged, exactly as push() does with the
    # buckets. VALUE_BY_FILE is a running total that report_value() only ever
    # adds to and nothing drained, so every flush re-sent every earlier
    # earning; /api/ingest/value is a pure INSERT, so each re-send was
    # permanent. A failed push keeps the total, so a retry can still send it.
    value_by_file.clear()
