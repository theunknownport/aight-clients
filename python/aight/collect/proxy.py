"""Report LLM spend for any agent, by standing between it and the API.

Every other collector here reads an agent's own record of what it did, which
means one parser per agent and no coverage at all for an agent whose record is
unreadable — Cursor keeps no per-call token counts on disk, and Amp emits no
model name anywhere in its stream. This one does not need to know what the
agent is: it sees the request and the response, so it works for anything that
lets you point its base URL somewhere else.

    pip install aight
    aight-proxy                      # listens on 127.0.0.1:8787
    export OPENAI_BASE_URL=http://127.0.0.1:8787/openai
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic

The path prefix picks the upstream, because both APIs live under /v1/ and
/v1/messages would otherwise be ambiguous. Everything after the prefix is
forwarded unchanged, including your Authorization header. This process holds no
credentials of its own and stores nothing from the request or the response —
only the model, the token counts and the latency.

It measures latency, which the SDKs cannot. They are called after the call
returns, so a duration taken there would time the recording; this sits around
the call, so `latency_ms` is the real thing.

Two things about the numbers are easy to get wrong in opposite directions:

- OpenAI's `prompt_tokens` *includes* `prompt_tokens_details.cached_tokens`.
  AIght's `input_tokens` is the uncached prompt, so the cached count is
  subtracted here. Sending the inclusive number bills the cached tokens twice,
  permanently — cost is frozen at receive time and there is no repair path.
- Anthropic reports the split directly: its `input_tokens` is already the
  uncached prompt, with the read and creation counts alongside. Nothing is
  subtracted for Anthropic, and subtracting would under-report just as badly.

A streaming OpenAI response carries `usage` only if the request asked for it
(`stream_options: {"include_usage": true}`), and most agents do not. The proxy
adds that to streaming requests. That is a deliberate change to the request
rather than a passive read, and it is the lesser evil: without it a streaming
agent — which is most of them — reports no spend at all, and silent
under-reporting is worse than one extra final chunk that every OpenAI client
tolerates. Anthropic needs no equivalent; its stream carries usage unprompted.

Cost is not computed here. The platform prices every row at receive time and
that number is definitive, so rows go up with cost_usd 0.0.
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import sys
import time
import urllib.error
from datetime import UTC, datetime

from .claude_code import DEFAULT_INGEST_URL, push

UPSTREAMS = {
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def usage_from_openai(body: dict) -> dict | None:
    """Token counts from an OpenAI response body, or None if it has none.

    The subtraction is the whole point of this function. `prompt_tokens` counts
    the cached prefix as part of the prompt and AIght's `input_tokens` does not,
    so the two conventions are reconciled here — once, at the edge, where the
    difference is still visible.

    A response with no `usage` — an error body, or a model that omits it —
    returns None rather than a row of zeros, because a row of zeros is a claim
    that the call was free.
    """
    usage = (body or {}).get("usage") or {}
    if not usage:
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if not (prompt or completion or cached):
        return None
    return {
        # max(..., 0) rather than a bare subtraction: a provider reporting
        # cached > prompt would otherwise send a negative count, and nothing
        # downstream is prepared to price one.
        "input_tokens": max(prompt - cached, 0),
        "output_tokens": completion,
        "cache_read_tokens": cached,
        # OpenAI has no cache *write* charge; that is Anthropic's model.
        "cache_creation_tokens": 0,
    }


def usage_from_anthropic(body: dict) -> dict | None:
    """Token counts from an Anthropic response body, or None if it has none.

    No arithmetic. Anthropic already reports the uncached prompt in
    `input_tokens` and the cache traffic beside it, which is the shape AIght
    wants. Subtracting here — copying the OpenAI path out of habit — would take
    the cache reads off the prompt a second time and under-report every
    cache-heavy call.
    """
    usage = (body or {}).get("usage") or {}
    if not usage:
        return None
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
    }


def usage_from_sse(raw: bytes, upstream: str) -> dict | None:
    """Token counts from a streamed response, or None if it carried none.

    Both providers put usage in the event stream, in different places and with
    different completeness, so the two shapes are read separately:

    - OpenAI sends it once, in the final chunk whose `choices` is empty, and
      only when the request asked for it.
    - Anthropic splits it: `message_start` carries the input and cache counts
      and `message_delta` carries the output count, arriving at the end. So the
      two are merged rather than the last one winning.

    A parse failure mid-stream is not fatal — half a stream still holds a
    usable count, and this runs inside somebody's request path.
    """
    merged: dict = {}
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        if upstream == "openai":
            found = usage_from_openai(event)
            if found:
                merged.update(found)
        else:
            usage = event.get("usage") or (event.get("message") or {}).get("usage") or {}
            if usage:
                # Anthropic's events are partial, so a zero from one of them is
                # "not stated here" rather than "zero" — hence the `if value`.
                for key, value in usage_from_anthropic({"usage": usage}).items():
                    if value:
                        merged[key] = value
    return merged or None


def model_from_sse(raw: bytes) -> str:
    """The first model name any event in the stream mentions.

    OpenAI repeats it on every chunk; Anthropic names it once, on
    `message_start`. Taking the first rather than the last means a stream that
    switched models mid-flight is attributed to the one that was asked for,
    which is the one the prompt was priced against.
    """
    for line in raw.split(b"\n"):
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip() or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        for candidate in (event.get("model"), (event.get("message") or {}).get("model")):
            if isinstance(candidate, str) and candidate:
                return candidate
    return ""


def row_for(agent: str, path: str, model: str, usage: dict, latency_ms: float | None) -> dict:
    """One ingest row for one proxy-handled call.

    External, not internal: the proxy sees an HTTP request, not a stack, so
    there is no line of anybody's code to charge and frame 1 carries the
    endpoint instead. That is the same reason the Claude Code collector's rows
    are external — a fact about the data rather than a setting.
    """
    return {
        "chain": [
            {"filepath": agent, "lineno": 0, "function": agent},
            {"filepath": "", "lineno": 0, "function": path},
        ],
        "kind": "external",
        "model": model,
        "calls": 1,
        "cost_usd": 0.0,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_creation_tokens": usage["cache_creation_tokens"],
        "latency_ms": latency_ms,
        "trace_id": "",
    }


def include_usage(body: bytes) -> bytes | None:
    """`body` with stream_options.include_usage set — or None if it cannot be.

    None means the caller forwards the body untouched, which is the right
    answer for anything that is not a JSON object: a malformed body is the
    upstream's to reject, with its own error message, not ours to rewrite.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    options = parsed.get("stream_options")
    if not isinstance(options, dict):
        options = {}
    options["include_usage"] = True
    parsed["stream_options"] = options
    return json.dumps(parsed).encode()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "aight-proxy"

    # Set by serve(). http.server instantiates the handler per request, so this
    # is the only way to hand it configuration.
    api_key = ""
    ingest_url = DEFAULT_INGEST_URL
    agent = "proxy"

    def log_message(self, fmt, *args):
        # Off: the point of this process is to be invisible, and a line per
        # request would drown whatever the user is actually running.
        pass

    def _split_path(self) -> tuple[str | None, str]:
        """(upstream key, path to forward) from the requested path.

        The prefix is what disambiguates the two APIs, so an unknown one is a
        client-side configuration mistake, answered as such rather than guessed
        at.
        """
        prefix, _, rest = self.path.lstrip("/").partition("/")
        return (prefix if prefix in UPSTREAMS else None), "/" + rest

    def _forward_headers(self) -> dict:
        """The client's headers, minus the ones that describe *this* hop.

        Host must become the upstream's, or it gets a 400. Connection and the
        other hop-by-hop headers are dropped by the spec's own rules.
        """
        drop = {"host", "content-length", "connection", "keep-alive",
                "proxy-connection", "transfer-encoding", "upgrade"}
        return {k: v for k, v in self.headers.items() if k.lower() not in drop}

    def _handle(self, method: str) -> None:
        upstream_key, path = self._split_path()
        if upstream_key is None:
            self.send_error(
                404,
                f"unknown upstream prefix; expected one of /{'|/'.join(sorted(UPSTREAMS))}",
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = self._forward_headers()

        # The one request rewrite in this file, and the reason is in the module
        # docstring: without it a streaming OpenAI call reports nothing at all.
        if upstream_key == "openai" and b'"stream":true' in body.replace(b" ", b""):
            rewritten = include_usage(body)
            if rewritten is not None:
                body = rewritten
        headers["Content-Length"] = str(len(body))

        started = time.perf_counter()
        connection = http.client.HTTPSConnection(UPSTREAMS[upstream_key], timeout=600)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            elapsed_ms = (time.perf_counter() - started) * 1000
            model, usage = self._relay(response, upstream_key)
        except OSError as e:
            # The client's failure to reach us and its failure to reach the
            # provider are different problems, and it is about to have one of
            # them — so say which.
            self.send_error(502, f"upstream {UPSTREAMS[upstream_key]} unreachable: {e}")
            return
        finally:
            connection.close()

        # After the response is on the wire, so the client never waits on this.
        self._report(path, model, usage, elapsed_ms)

    def _relay(self, response, upstream_key: str) -> tuple[str, dict | None]:
        """Send the upstream response to the client, keeping what is needed.

        A streaming response is forwarded event by event rather than buffered:
        the client asked for a stream and got one, and holding it back to read
        usage out of it would defeat the request. Non-streaming bodies are read
        whole, because there is nothing to preserve by doing otherwise.
        """
        self.send_response(response.status)
        content_type = response.headers.get("Content-Type", "")
        for key, value in response.headers.items():
            if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(key, value)

        if "text/event-stream" not in content_type:
            raw = response.read()
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
            return self._interpret(raw, upstream_key)

        # Streaming: no Content-Length, and each event flushed as it arrives.
        # Buffering the stream to parse it would turn a streaming agent into a
        # non-streaming one, which is a worse outcome than a missing row.
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.end_headers()
        chunks: list[bytes] = []
        while True:
            line = response.readline()
            if not line:
                break
            chunks.append(line)
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # The agent hung up mid-stream — its own timeout, or Ctrl-C.
                # The reply is gone but the spend is real and already incurred,
                # so stop writing and keep reading.
                chunks.append(response.read())
                break
        return self._interpret(b"".join(chunks), upstream_key)

    def _interpret(self, raw: bytes, upstream_key: str) -> tuple[str, dict | None]:
        """(model, usage) out of whatever came back — stream, JSON, or neither."""
        if not raw:
            return "", None
        if raw.lstrip().startswith(b"data:") or b"\ndata:" in raw:
            return model_from_sse(raw), usage_from_sse(raw, upstream_key)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # An HTML error page from a gateway, say. Nothing to attribute.
            return "", None
        usage = (
            usage_from_openai(body) if upstream_key == "openai" else usage_from_anthropic(body)
        )
        return str((body or {}).get("model") or ""), usage

    def _report(self, path: str, model: str, usage: dict | None, latency_ms: float) -> None:
        """Push one row, or say why not.

        A response with no usage is skipped rather than sent as zeros — there
        is no honest row to send, and zeros would claim the call was free.
        """
        if not usage:
            return
        if not model:
            print(f"{path}: response named no model, nothing to attribute", file=sys.stderr)
            return
        row = row_for(self.agent, path, model, usage, latency_ms)
        try:
            push([row], self.api_key, self.ingest_url, language="aight-proxy")
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            # Never fatal: this sits inside somebody's request path, and a
            # failed report must not become a failed LLM call.
            print(f"Could not report {model}: {e}", file=sys.stderr)
            return
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        print(f"[{stamp}] {model} {usage['input_tokens']}in/{usage['output_tokens']}out "
              f"{latency_ms:.0f}ms", flush=True)

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        # Some clients probe /v1/models before anything else. Forwarding is the
        # whole job; there is nothing to attribute to a model list.
        self._handle("GET")


def serve(host: str, port: int, api_key: str, ingest_url: str, agent: str) -> int:
    # Threading, because an agent may hold a stream open for minutes while it
    # makes another call, and a single-threaded server would queue that second
    # call behind the first — adding latency to the very thing being measured.
    Handler.api_key = api_key
    Handler.ingest_url = ingest_url
    Handler.agent = agent
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"aight-proxy on http://{host}:{port} — reporting as {agent!r}. Ctrl-C to stop.")
    print(f"  export OPENAI_BASE_URL=http://{host}:{port}/openai")
    print(f"  export ANTHROPIC_BASE_URL=http://{host}:{port}/anthropic")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aight-proxy",
        description="Report LLM spend for any agent, by standing between it and the API.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"interface to listen on (default: {DEFAULT_HOST} — "
                             f"loopback, because this sees your API keys)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--agent", default=os.environ.get("AIGHT_PROXY_AGENT", "proxy"),
                        help="what to call the agent these calls belong to, so several "
                             "agents through one proxy stay distinct "
                             "(env: AIGHT_PROXY_AGENT)")
    args = parser.parse_args(argv)

    api_key = os.environ.get("AIGHT_API_KEY")
    if not api_key:
        print("Set AIGHT_API_KEY (Integrate tab of your AIght Workspace)", file=sys.stderr)
        return 1
    ingest_url = os.environ.get("AIGHT_INGEST_URL", DEFAULT_INGEST_URL)
    return serve(args.host, args.port, api_key, ingest_url, args.agent)


def cli() -> None:
    """Console entry point: `aight-proxy`."""
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0) from None


if __name__ == "__main__":
    cli()
