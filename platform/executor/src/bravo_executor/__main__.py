"""``python -m bravo_executor``: serve the Executor API (host/port from BRAVO_EXECUTOR_HOST/PORT)."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app
from .telemetry import setup_tracing


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    setup_tracing("executor")
    host = os.environ.get("BRAVO_EXECUTOR_HOST", "0.0.0.0")  # noqa: S104 - container listens on all interfaces
    port = int(os.environ.get("BRAVO_EXECUTOR_PORT", "8081"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
