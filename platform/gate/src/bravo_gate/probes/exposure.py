"""NET-EXTERNAL-SCAN (NET-01, spec §8.5 "외부 포트 스캔에 공개 진입점만 보임", nmap).

From the gate process on the platform (outside every customer network), a TCP connect scan of each registered public
address of the environment over a fixed port list: the same idea as ``nmap -sT -Pn -p<list>`` (full connect, no ping),
written here so the gate image needs no raw-socket capability. A port is ``open`` (connect succeeded), ``closed``
(refused) or ``filtered`` (timeout). Every resolved IPv4 address of a name is scanned.

Verdict:
    FAIL     an open port that the endpoint does not allow (``unexpected_open_port``)
    UNKNOWN  no endpoint registered, a name does not resolve, or no allowed port answered anywhere (the scanner may
             not reach the internet: nothing was proven), or a port gave another error
    PASS     every open port is allowed and at least one allowed port answered (the scan reached the environment)
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..environments import NetworkSection
from ..result import Obs, error_text, failed, passed, unknown


def _connect(addr: str, port: int, timeout_s: float) -> str:
    try:
        with socket.create_connection((addr, port), timeout=timeout_s):
            return "open"
    except TimeoutError:
        return "filtered"
    except ConnectionRefusedError:
        return "closed"
    except OSError as exc:
        return f"error:{type(exc).__name__}"


def resolve(host: str) -> list[str]:
    return sorted({a[4][0] for a in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)})


def scan(section: NetworkSection, *, resolver=resolve, connect=_connect) -> Obs:
    if not section.public_endpoints:
        return unknown("public_endpoints_not_configured")
    ports = tuple(sorted(set(section.scan_ports)))
    rows: list[dict[str, Any]] = []
    jobs: list[tuple[int, str, int]] = []
    for i, ep in enumerate(section.public_endpoints):
        try:
            addrs = resolver(ep.host)
        except Exception as exc:  # noqa: BLE001
            rows.append({"host": ep.host, "kind": ep.kind, "error": error_text(exc)})
            continue
        rows.append({"host": ep.host, "kind": ep.kind, "allow": sorted(ep.allow), "addresses": addrs,
                     **({"note": ep.note} if ep.note else {})})
        jobs.extend((i, a, p) for a in addrs for p in ports)
    with ThreadPoolExecutor(max_workers=64) as pool:
        states = list(pool.map(lambda j: connect(j[1], j[2], section.scan_timeout_s), jobs))
    by_row: dict[int, dict[str, dict[str, list[int]]]] = {}
    for (i, addr, port), state in zip(jobs, states, strict=True):
        by_row.setdefault(i, {}).setdefault(addr, {}).setdefault(state, []).append(port)
    unexpected: list[str] = []
    errors: list[str] = []
    allowed_open = 0
    for i, ep in enumerate(section.public_endpoints):
        row = next((r for r in rows if r["host"] == ep.host and "addresses" in r), None)
        if row is None:
            continue
        open_ports = sorted({p for st in by_row.get(i, {}).values() for p in st.get("open", [])})
        row["open"] = open_ports
        row["by_address"] = {a: {k: sorted(v) for k, v in st.items() if k != "filtered"}
                             for a, st in sorted(by_row.get(i, {}).items())}
        allowed_open += len([p for p in open_ports if p in ep.allow])
        unexpected += [f"{ep.host}:{p}" for p in open_ports if p not in ep.allow]
        errors += [f"{ep.host}:{p}" for st in by_row.get(i, {}).values() for k, ps in st.items()
                   if k.startswith("error:") for p in ps]
    ev = {"method": "tcp connect scan from the platform (nmap -sT -Pn equivalent)", "ports": list(ports),
          "timeout_s": section.scan_timeout_s, "endpoints": rows}
    if unexpected:
        return failed("unexpected_open_port", unexpected=sorted(unexpected), **ev)
    if any("error" in r for r in rows):
        return unknown("endpoint_unresolved", **ev)
    if errors:
        return unknown("scan_error", errors=sorted(errors)[:20], **ev)
    if allowed_open == 0:
        return unknown("nothing_answered", **ev)  # the scanner may not reach the internet; nothing is proven
    return passed("only_published_entry_points", **ev)
