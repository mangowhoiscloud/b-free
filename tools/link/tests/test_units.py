"""Pure parts: argv, backoff, address parsing, status file, rendered manifest drift."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bravo_link import LINK_LABEL
from bravo_link.agent import parse_addr
from bravo_link.forward import ForwardSpec, backoff_s
from bravo_link.manifest import RelaySpec, render
from bravo_link.status import LinkStatus, StatusWriter

K8S = Path(__file__).parents[1] / "k8s" / "reverse-relay.yaml"


def test_port_forward_binds_loopback_by_default() -> None:
    spec = ForwardSpec("bravo-source", "chat", "svc/chat-db-rw", 5432, 15432)
    assert spec.argv() == ["kubectl", "--context", "bravo-source", "-n", "chat", "port-forward",
                           "--address", "127.0.0.1", "svc/chat-db-rw", "15432:5432"]
    assert ForwardSpec("c", "n", "svc/x", 1, 2, bind_address="0.0.0.0").connect_host == "127.0.0.1"  # noqa: S104


def test_backoff_is_capped_exponential() -> None:
    assert [backoff_s(i) for i in (1, 2, 3, 4, 6, 10)] == [1, 2, 4, 8, 30, 30]


def test_parse_addr() -> None:
    assert parse_addr("host.lima.internal:15432") == ("host.lima.internal", 15432)
    for bad in ("nohost", ":1", "h:"):
        with pytest.raises(ValueError):
            parse_addr(bad)


def test_status_file_is_labelled_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "s" / "link.json"
    st = LinkStatus(kind="forward", name="src-db", detail={"local": "127.0.0.1:15432"})
    StatusWriter(path).write(st)
    data = json.loads(path.read_text())
    assert data["label"] == LINK_LABEL == "임시 링크(D-16 전)"
    assert data["restarts"] == 0 and "updated_at" in data
    assert [p.name for p in path.parent.iterdir()] == ["link.json"]


def test_committed_manifest_matches_renderer() -> None:
    assert K8S.read_text(encoding="utf-8") == render(RelaySpec())
    text = K8S.read_text(encoding="utf-8")
    assert '"--agent-bind", "127.0.0.1"' in text and "name: dst-db-link" in text
