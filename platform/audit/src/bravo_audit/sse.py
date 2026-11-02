"""Minimal Server-Sent Events reader (WHATWG event-stream rules) for the platform API event stream."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SseEvent:
    id: str | None
    event: str
    data: str


def parse(lines: Iterable[str]) -> Iterator[SseEvent]:
    """Yield events from decoded lines. Comment lines (``:ping``) and empty data are skipped."""
    eid: str | None = None
    name = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if data:
                yield SseEvent(eid, name, "\n".join(data))
            name, data = "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            data.append(value)
        elif field == "id":
            eid = value
        elif field == "event":
            name = value
    if data:
        yield SseEvent(eid, name, "\n".join(data))
