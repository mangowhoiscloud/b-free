"""OpenTelemetry setup: OTLP gRPC exporter when OTEL_EXPORTER_OTLP_ENDPOINT is set."""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

LOG = logging.getLogger("bravo_api.telemetry")
_configured = False


def setup(service_name: str) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})
    provider = TracerProvider(resource=resource)
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        LOG.info("OTLP trace export enabled")
    trace.set_tracer_provider(provider)
