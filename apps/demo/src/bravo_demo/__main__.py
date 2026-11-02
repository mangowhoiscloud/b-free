"""Entry point: ``python -m bravo_demo``. All settings come from the environment (config.py).

Two listeners in one process: the chat (APP_PORT, behind the edge) and the write-block switch
(ADMIN_PORT, cluster-internal only). On SIGTERM/SIGINT both stop; the chat listener closes its
WebSockets with 1012 so browsers reconnect with their last message id.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Generator

import uvicorn

from . import config
from .telemetry import setup_logging, setup_otel, shutdown_otel


class _Server(uvicorn.Server):
    # Signals are handled once for both servers in _serve(); uvicorn's own capture would let
    # only the last server installed see SIGTERM.
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


async def _serve(settings: config.Settings) -> None:
    from .app import create_app

    app = create_app(settings)
    common = {
        "ws": "websockets-sansio",
        "proxy_headers": True,
        "forwarded_allow_ips": settings.forwarded_allow_ips,
        "timeout_graceful_shutdown": 10,
        "log_config": None,
        "access_log": False,
        "server_header": False,
    }
    chat = _Server(uvicorn.Config(
        app, host=settings.host, port=settings.port, ws_ping_interval=20.0, ws_ping_timeout=20.0,
        ws_max_size=65_536, **common,
    ))
    admin = _Server(uvicorn.Config(
        app.state.admin_app, host=settings.admin.host, port=settings.admin.port, lifespan="off", **common,
    ))

    loop = asyncio.get_running_loop()

    def stop(sig: signal.Signals) -> None:
        if chat.should_exit and sig == signal.SIGINT:
            chat.force_exit = admin.force_exit = True
        chat.should_exit = admin.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop, sig)

    servers = [asyncio.create_task(chat.serve(), name="chat"), asyncio.create_task(admin.serve(), name="admin")]
    done, _ = await asyncio.wait(servers, return_when=asyncio.FIRST_COMPLETED)
    # One listener ended (signal, or it failed to bind): stop the other one too.
    chat.should_exit = admin.should_exit = True
    await asyncio.gather(*servers, return_exceptions=True)
    for task in done:
        if task.exception() is not None:
            raise task.exception()  # type: ignore[misc]
    if not chat.started or not admin.started:
        raise SystemExit(1)


def main() -> int:
    config.apply_sdk_defaults()
    try:
        settings = config.load()
    except (config.ConfigError, OSError) as exc:
        print(f"bravo-demo: configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings)
    setup_otel(settings)
    try:
        asyncio.run(_serve(settings))
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        shutdown_otel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
