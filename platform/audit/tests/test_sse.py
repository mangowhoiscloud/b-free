"""SSE parsing of the platform API stream format (sse-starlette: id, data, blank line; ``: ping`` comments)."""

from __future__ import annotations

import json

from bravo_audit.sse import parse

STREAM = (
    "id: 1\r\ndata: {\"id\":1,\"kind\":\"state.changed\"}\r\n\r\n"
    ": ping - 2026-11-02 11:00:00\r\n\r\n"
    "id: 2\r\ndata: {\"id\":2,\"kind\":\"gate.verdict\"}\r\n\r\n"
    "id: 3\r\nevent: message\r\ndata: {\"id\":3,\r\ndata: \"kind\":\"tool.denied\"}\r\n\r\n"
)


def test_parse_ids_data_and_skips_pings() -> None:
    events = list(parse(STREAM.splitlines(keepends=True)))
    assert [e.id for e in events] == ["1", "2", "3"]
    assert [json.loads(e.data)["kind"] for e in events] == ["state.changed", "gate.verdict", "tool.denied"]


def test_trailing_event_without_blank_line() -> None:
    events = list(parse(["id: 9", "data: {}"]))
    assert len(events) == 1 and events[0].id == "9"
