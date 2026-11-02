"""bravo-link: the temporary link 임시 링크(D-16 전) (CLOUD-E2E-CONTRACT.md §7).

Subcommands:
  forward       supervise kubectl port-forward laptop -> cluster (forward.py)
  reverse       laptop agent of the reverse tunnel cluster -> laptop -> K3s (agent.py)
  relay         the in-cluster relay (relay.py; in the pod it runs as `python3 relay.py`)
  render-relay  print the relay manifest (manifest.py)
  probe-relay   print relay stats through its agent port
Each subcommand has --help. Status files hold no secrets; DSNs are read from env vars only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path

from . import relay as relay_mod
from .agent import ReverseAgent, parse_addr, probe_relay
from .forward import ForwardSpec, PortForward
from .manifest import RelaySpec, render
from .status import StatusWriter


def _status(path: str | None) -> StatusWriter:
    value = path or os.environ.get("BRAVO_LINK_STATUS_FILE")
    return StatusWriter(Path(value) if value else None)


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    return stop


def _add_forward_args(p: argparse.ArgumentParser, prefix: str = "") -> None:
    dash = f"--{prefix}-" if prefix else "--"
    p.add_argument(f"{dash}context", help="kubectl context")
    p.add_argument(f"{dash}namespace", default="chat")
    p.add_argument(f"{dash}resource", help="svc/NAME, pod/NAME or deploy/NAME")


async def _forward(args: argparse.Namespace) -> int:
    spec = ForwardSpec(args.context, args.namespace, args.resource, args.remote_port, args.local_port,
                       args.bind_address, args.kubectl)
    pf = PortForward(spec, _status(args.status_file), args.name or f"forward-{args.local_port}", args.probe,
                     args.health_dsn_env, args.interval, args.timeout, args.fail_threshold)
    await pf.run(_stop_event())
    return 0


async def _reverse(args: argparse.Namespace) -> int:
    stop = _stop_event()
    forwards: list[PortForward] = []
    status_dir = Path(args.status_file).parent if args.status_file else None

    def sub_status(suffix: str) -> StatusWriter:
        return StatusWriter(status_dir / f"{Path(args.status_file).stem}-{suffix}.json" if status_dir else None)

    if args.relay_context:
        spec = ForwardSpec(args.relay_context, args.relay_namespace, args.relay_resource,
                           args.relay_agent_port, args.relay_local_port, "127.0.0.1", args.kubectl)
        forwards.append(PortForward(spec, sub_status("relay"), "relay-agent-port", "tcp"))
        relay_addr = ("127.0.0.1", args.relay_local_port)
    elif args.relay_addr:
        relay_addr = parse_addr(args.relay_addr)
    else:
        raise SystemExit("give --relay-addr or --relay-context/--relay-resource")

    if args.target_context:
        spec = ForwardSpec(args.target_context, args.target_namespace, args.target_resource,
                           args.target_remote_port, args.target_local_port, "127.0.0.1", args.kubectl)
        forwards.append(PortForward(spec, sub_status("target"), "target", args.target_probe))
        target = ("127.0.0.1", args.target_local_port)
    elif args.target:
        target = parse_addr(args.target)
    else:
        raise SystemExit("give --target or --target-context/--target-resource")

    agent = ReverseAgent(relay_addr, target, args.pool, _status(args.status_file), args.name or "reverse",
                         args.ping_interval, args.connect_timeout)
    tasks = [asyncio.ensure_future(f.run(stop)) for f in forwards]
    tasks.append(asyncio.ensure_future(agent.run(stop)))
    await stop.wait()
    await asyncio.gather(*tasks, return_exceptions=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bravo-link", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    from . import forward as forward_mod

    f = sub.add_parser("forward", help="supervised kubectl port-forward", description=forward_mod.__doc__,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_forward_args(f)
    f.add_argument("--remote-port", type=int, required=True)
    f.add_argument("--local-port", type=int, required=True)
    f.add_argument("--bind-address", default="127.0.0.1",
                   help="127.0.0.1 (default; Lima guests reach it as host.lima.internal). Never 0.0.0.0 by default")
    f.add_argument("--probe", choices=("pg", "tcp"), default="tcp")
    f.add_argument("--health-dsn-env", help="env var holding a libpq DSN for SELECT 1 (value never logged)")
    f.add_argument("--interval", type=float, default=5.0)
    f.add_argument("--timeout", type=float, default=5.0)
    f.add_argument("--fail-threshold", type=int, default=3)
    f.add_argument("--status-file")
    f.add_argument("--name")
    f.add_argument("--kubectl", default="kubectl")

    from . import agent as agent_mod

    r = sub.add_parser("reverse", help="laptop agent of the reverse tunnel", description=agent_mod.__doc__,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    r.add_argument("--relay-addr", help="host:port of the relay agent port (already forwarded)")
    _add_forward_args(r, "relay")
    r.add_argument("--relay-agent-port", type=int, default=7070)
    r.add_argument("--relay-local-port", type=int, default=17070)
    r.add_argument("--target", help="host:port the paired connections go to")
    _add_forward_args(r, "target")
    r.add_argument("--target-remote-port", type=int, default=5432)
    r.add_argument("--target-local-port", type=int, default=25432)
    r.add_argument("--target-probe", choices=("pg", "tcp"), default="pg")
    r.add_argument("--pool", type=int, default=8, help="idle connections kept at the relay")
    r.add_argument("--ping-interval", type=float, default=10.0)
    r.add_argument("--connect-timeout", type=float, default=5.0)
    r.add_argument("--status-file")
    r.add_argument("--name")
    r.add_argument("--kubectl", default="kubectl")

    rl = sub.add_parser("relay", help="in-cluster relay (same as python3 relay.py)",
                        description=relay_mod.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    relay_mod.build_parser(rl)

    m = sub.add_parser("render-relay", help="print the relay manifest")
    m.add_argument("--namespace", default="chat")
    m.add_argument("--name", default="dst-db-link")
    m.add_argument("--image", default="bravo-link-relay", help="python3 image; default is the logical kustomize name")
    m.add_argument("--client-port", type=int, default=5432)
    m.add_argument("--agent-port", type=int, default=7070)

    pr = sub.add_parser("probe-relay", help="relay stats via its agent port")
    pr.add_argument("--relay-addr", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)
    if args.cmd == "relay":
        asyncio.run(relay_mod.serve(args))
        return
    if args.cmd == "render-relay":
        sys.stdout.write(render(RelaySpec(args.namespace, args.name, args.image, args.client_port, args.agent_port)))
        return
    if args.cmd == "probe-relay":
        print(json.dumps(asyncio.run(probe_relay(parse_addr(args.relay_addr)))))
        return
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(_forward(args) if args.cmd == "forward" else _reverse(args)))


if __name__ == "__main__":
    main()
