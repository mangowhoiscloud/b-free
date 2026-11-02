"""OTLP metrics for the app-mode load (output contract: tools/loadgen/README.md, "지표 계약").

Pushed over OTLP/HTTP to the platform OTel Collector, which writes them to the platform Prometheus.
Names after the Prometheus OTLP translation (verified on the platform Prometheus, 2026-11-03):

  bravo_loadgen_attempts_total{run_id, op, result, code, served_by}
  bravo_loadgen_attempt_duration_milliseconds_{bucket,count,sum}{run_id, op, result, served_by}
  bravo_loadgen_request_duration_milliseconds_{bucket,count,sum}{run_id, op, outcome, served_by}

Resource: service.name=bravo-loadgen (label job), service.instance.id=<run id>/w<worker> (label
instance, one per worker process), bravo.plan_id=<plan id> when given (label bravo_plan_id). Sum over
instance for a run. Without an endpoint every call is a no-op.
"""

from __future__ import annotations

from typing import Any

# Explicit buckets (ms). The laptop path through the Cloudflare edge is ~300 ms, a write pause is seconds.
BUCKETS_MS = (25, 50, 100, 200, 300, 400, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000, 15000, 30000)


class LoadMetrics:
    def __init__(self, endpoint: str | None, run_id: str, plan_id: str | None, target: str,
                 interval_s: float, instance_id: str | None = None) -> None:
        self._run_id = run_id
        self._provider: Any = None
        self._attempts: Any = None
        self._attempt_ms: Any = None
        self._request_ms: Any = None
        if not endpoint:
            return
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
        from opentelemetry.sdk.resources import Resource

        attrs: dict[str, str] = {
            "service.name": "bravo-loadgen",
            "service.instance.id": instance_id or run_id,
            "bravo.loadgen.target": target,
        }
        if plan_id:
            attrs["bravo.plan_id"] = plan_id
        url = endpoint.rstrip("/")
        if not url.endswith("/v1/metrics"):
            url += "/v1/metrics"
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=url, timeout=5), export_interval_millis=int(interval_s * 1000)
        )
        hist = ExplicitBucketHistogramAggregation(boundaries=BUCKETS_MS)
        self._provider = MeterProvider(
            resource=Resource.create(attrs),
            metric_readers=[reader],
            views=[
                View(instrument_name="bravo.loadgen.attempt.duration", aggregation=hist),
                View(instrument_name="bravo.loadgen.request.duration", aggregation=hist),
            ],
        )
        meter = self._provider.get_meter("bravo_loadgen")
        self._attempts = meter.create_counter("bravo.loadgen.attempts", unit="{attempt}",
                                              description="every write attempt and every read")
        self._attempt_ms = meter.create_histogram("bravo.loadgen.attempt.duration", unit="ms",
                                                  description="one HTTP attempt or one WebSocket history read")
        self._request_ms = meter.create_histogram("bravo.loadgen.request.duration", unit="ms",
                                                  description="logical request, scheduled time to final outcome")

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    def attempt(self, op: str, ok: bool, code: str, served_by: str, ms: float) -> None:
        if self._provider is None:
            return
        result = "ok" if ok else "error"
        base = {"run_id": self._run_id, "op": op, "result": result, "served_by": served_by}
        self._attempts.add(1, {**base, "code": code})
        self._attempt_ms.record(ms, base)

    def request(self, op: str, outcome: str, served_by: str, ms: float) -> None:
        if self._provider is None:
            return
        self._request_ms.record(ms, {"run_id": self._run_id, "op": op, "outcome": outcome, "served_by": served_by})

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.force_flush(timeout_millis=10_000)
            self._provider.shutdown()
