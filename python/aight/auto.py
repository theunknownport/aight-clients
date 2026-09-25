"""Auto-instrumentation: patches supported LLM provider clients so every
real call is traced without wrapping each one in traced_llm_call().

Each provider's OTel instrumentor package is optional. Install the extra
for whatever you use (`aight[openai]`, `aight[anthropic]`, from the git URL
in the README — this SDK is not on PyPI); anything not installed is skipped,
not an error, so a customer using only one provider never has to install the
other's instrumentor.

Auto-instrumented calls carry no latency. A duration is reported only when
the caller measures the call and passes it to traced_llm_call(), and an
instrumentor has none to pass — so those rows show "—" on the Speed axis
rather than a number nobody took.
"""
from __future__ import annotations

import importlib
import logging

logger = logging.getLogger("aight.auto")

_INSTRUMENTORS = {
    "openai": ("opentelemetry.instrumentation.openai", "OpenAIInstrumentor"),
    "anthropic": ("opentelemetry.instrumentation.anthropic", "AnthropicInstrumentor"),
}

_instrumented: set[str] = set()


def auto_instrument(providers: list[str] | None = None) -> list[str]:
    """Instrument each named provider's client library (default: every
    known provider). Returns the providers actually instrumented this call.
    A provider whose instrumentor package isn't installed, or that's
    already instrumented, is silently skipped."""
    names = providers if providers is not None else list(_INSTRUMENTORS)
    done = []
    for name in names:
        if name in _instrumented:
            continue
        target = _INSTRUMENTORS.get(name)
        if target is None:
            logger.warning("aight: unknown provider %r, skipping", name)
            continue
        module_name, class_name = target
        try:
            module = importlib.import_module(module_name)
            instrumentor_cls = getattr(module, class_name)
        except (ImportError, AttributeError):
            continue
        instrumentor_cls().instrument()
        _instrumented.add(name)
        done.append(name)
    return done
