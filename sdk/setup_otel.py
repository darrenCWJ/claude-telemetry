"""
Shared OTEL provider setup for long-running Claude API applications.

Usage:
    from sdk.setup_otel import setup_otel

    tp, mp, lp = setup_otel("my-app")
    # providers are now registered globally; use trace.get_tracer(), etc.
"""

import os
import logging
from typing import Tuple

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter


def setup_otel(
    service_name: str,
    otel_endpoint: str | None = None,
) -> Tuple[TracerProvider, MeterProvider, LoggerProvider]:
    endpoint = otel_endpoint or os.environ.get("OTEL_ENDPOINT", "http://localhost:4318")

    resource = Resource.create({
        SERVICE_NAME: service_name,
        "host.name": os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
    })

    # Traces — batch for throughput in long-running processes
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
    ))
    trace.set_tracer_provider(tp)

    # Metrics — export every 15 s
    mp = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
            export_interval_millis=15_000,
        )],
    )
    metrics.set_meter_provider(mp)

    # Logs — bridge Python logging → OTEL
    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(BatchLogRecordProcessor(
        OTLPLogExporter(endpoint=f"{endpoint}/v1/logs")
    ))
    set_logger_provider(lp)

    handler = LoggingHandler(level=logging.DEBUG, logger_provider=lp)
    root = logging.getLogger()
    if not any(isinstance(h, LoggingHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    return tp, mp, lp
