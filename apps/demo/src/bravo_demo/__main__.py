"""Entry point: ``python -m bravo_demo``. All settings come from the environment (config.py)."""

from __future__ import annotations

import sys

import uvicorn

from . import config
from .telemetry import setup_logging, setup_otel, shutdown_otel


def main() -> int:
    config.apply_sdk_defaults()
    try:
        settings = config.load()
    except (config.ConfigError, OSError) as exc:
        print(f"bravo-demo: configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings)
    setup_otel(settings)
    from .app import create_app

    try:
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            ws="websockets-sansio",
            ws_ping_interval=20.0,
            ws_ping_timeout=20.0,
            ws_max_size=65_536,
            proxy_headers=True,
            forwarded_allow_ips=settings.forwarded_allow_ips,
            timeout_graceful_shutdown=10,
            log_config=None,
            access_log=False,
            server_header=False,
        )
    finally:
        shutdown_otel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
