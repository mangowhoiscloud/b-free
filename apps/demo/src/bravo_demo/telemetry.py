"""OpenTelemetry traces and metrics to OTEL_EXPORTER_OTLP_ENDPOINT (OTLP/gRPC), JSON logs to stdout."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import metrics, trace

from .config import Settings

_STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, static: dict[str, str]) -> None:
        super().__init__()
        self.static = static

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **self.static,
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            doc["trace_id"] = f"{span.trace_id:032x}"
            doc["span_id"] = f"{span.span_id:016x}"
        for key, value in vars(record).items():
            if key not in _STD and not key.startswith("_"):
                doc[key] = value
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False, default=str)


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter({"service": settings.service_name, "env": settings.env_id}))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level)
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_providers: list[Any] = []


def setup_otel(settings: Settings) -> None:
    """Install SDK providers with OTLP exporters when an endpoint is configured; otherwise no-op."""
    if not settings.otlp_endpoint or _providers:
        return
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.instance.id": settings.instance_id,
            "deployment.environment.name": settings.env_id,
            "bravo.env_label": settings.env_label,
        }
    )
    # Endpoint, TLS and headers follow the standard OTEL_EXPORTER_OTLP_* variables.
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=5), export_timeout_millis=5000))
    trace.set_tracer_provider(tracer_provider)
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(timeout=5), export_interval_millis=10_000, export_timeout_millis=5000
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)
    _providers.extend([tracer_provider, meter_provider])


def shutdown_otel() -> None:
    # Bounded: an unreachable collector must not hold up pod termination.
    for provider in _providers:
        try:
            if hasattr(provider, "force_flush"):
                provider.force_flush(3000)
            provider.shutdown()
        except Exception:  # noqa: S110 - best effort at exit
            pass
    _providers.clear()


class Instruments:
    def __init__(self) -> None:
        self.tracer = trace.get_tracer("bravo_demo")
        meter = metrics.get_meter("bravo_demo")
        self.write_ms = meter.create_histogram(
            "chat.write.duration", unit="ms", description="Write latency including any write-pause wait"
        )
        self.writes = meter.create_counter("chat.writes", description="Writes by kind and outcome")
        self.poll_ms = meter.create_histogram("chat.poll.duration", unit="ms", description="Fan-out poll latency")
        self.ws_connections = meter.create_up_down_counter("chat.ws.connections", description="Open WebSockets")
        self.attachments = meter.create_counter("chat.attachments", description="Attachment uploads by outcome")
        self.write_block_changes = meter.create_counter(
            "chat.write_block.changes", description="Write-block switch changes applied through the admin port"
        )
