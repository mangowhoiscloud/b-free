"""OpenTelemetry tracing. OTLP/gRPC to OTEL_EXPORTER_OTLP_ENDPOINT when it is set.

Spans carry run_id, plan_id, epoch, stage names, ms and effect resource/action names.
They never carry DSNs, passwords or row contents.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger(__name__)


def setup_tracing(default_service_name: str) -> TracerProvider:
    service_name = os.environ.get("OTEL_SERVICE_NAME", default_service_name)
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        insecure = endpoint.startswith("http://")
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure)))
        log.info("tracing: OTLP/gRPC exporter enabled")
    else:
        log.info("tracing: OTEL_EXPORTER_OTLP_ENDPOINT not set, spans are not exported")
    trace.set_tracer_provider(provider)
    return provider
