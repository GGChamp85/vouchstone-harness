"""OpenTelemetry-compatible observability (C8).

Real OpenTelemetry integration, not a logging wrapper dressed up as one:
spans use the actual `opentelemetry-api` tracer contract, so this SDK's
traces show up in any real OTel backend (Jaeger, Honeycomb, Datadog,
whatever the customer already runs) without a Vouchstone-specific
exporter or adapter.

`opentelemetry-api`/`opentelemetry-sdk` are optional extras (see
pyproject.toml's `otel` extra), not core dependencies -- most of this SDK
must keep working for a customer who doesn't want OTel at all. When
neither package is importable, every span here becomes a real no-op
(`contextlib.nullcontext`), not a fake span object pretending to record
data. That's the honest boundary: instrumentation code always runs
identically either way, but only actually exports spans when OTel is
installed and configured.
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterator, Optional

_tracer = None
_otel_available = False

try:
    from opentelemetry import trace as _otel_trace
    _otel_available = True
except ImportError:
    _otel_trace = None


def is_otel_available() -> bool:
    """True if opentelemetry-api is importable. Does NOT mean a real
    exporter is configured -- configure_telemetry() (or the host
    application's own OTel setup) still has to run for spans to go
    anywhere; without that, the OTel SDK's own default no-op tracer is
    used, which is standard OTel behavior, not something this module
    fakes."""
    return _otel_available


def configure_telemetry(
    service_name: str = "vouchstone-sdk", exporter: Optional[Any] = None,
    batch: bool = True,
) -> bool:
    """Optional convenience setup: a real TracerProvider + (if provided) a
    real SpanExporter. Returns False (and does nothing) if
    opentelemetry-sdk isn't installed -- callers who need telemetry should
    check the return value, not assume success. A host application that
    already configures its own OTel TracerProvider doesn't need to call
    this at all; get_tracer() below just asks opentelemetry.trace for
    whatever provider is globally registered, same as any other
    OTel-instrumented library.

    ``batch=True`` (default) uses BatchSpanProcessor -- the standard,
    correct choice in production: spans are buffered and exported on a
    timer/size threshold so telemetry doesn't add a network round-trip to
    every operation. ``batch=False`` uses SimpleSpanProcessor -- exports
    each span synchronously the instant it ends. Tests (and any caller
    that needs to inspect spans immediately after the operation that
    created them, e.g. via InMemorySpanExporter) should pass
    ``batch=False``; asserting on BatchSpanProcessor's buffered output
    without a manual force_flush() would be asserting on a race.
    """
    if not _otel_available:
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        processor = BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
        provider.add_span_processor(processor)
    _otel_trace.set_tracer_provider(provider)
    return True


def get_tracer(name: str = "vouchstone_sdk"):
    if not _otel_available:
        return None
    return _otel_trace.get_tracer(name)


@contextlib.contextmanager
def span(name: str, attributes: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
    """Start a span if OTel is available and configured; a real
    contextlib.nullcontext otherwise -- callers never need to branch on
    whether OTel is installed, `with span(...):` always works.

    record_exception/set_status_on_exception are disabled on the
    underlying OTel span: every call site in this SDK explicitly calls
    record_exception() itself before re-raising (see agent.py, forge.py),
    so leaving OTel's own automatic on-exit recording enabled would
    double up every exception event and status-set, not add coverage."""
    tracer = get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False,
    ) as current_span:
        if attributes:
            for key, value in attributes.items():
                if value is not None:
                    current_span.set_attribute(key, value)
        yield current_span


def record_exception(current_span: Any, exc: BaseException) -> None:
    """No-op if current_span is None (OTel unavailable) -- callers can
    call this unconditionally after a `with span(...) as s:` block."""
    if current_span is not None:
        current_span.record_exception(exc)
        try:
            from opentelemetry.trace import Status, StatusCode
            current_span.set_status(Status(StatusCode.ERROR, str(exc)))
        except ImportError:
            pass
