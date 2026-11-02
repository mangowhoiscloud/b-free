"""HTTP API of the collector against real inputs (registry file, postgres container, file tree)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bravo_collector.app import create_app
from bravo_collector.environments import load_registry


@pytest.fixture
def client(pg_server, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVO_TEST_APP_DSN", pg_server.collector_dsn)
    reg = tmp_path / "environments.yaml"
    reg.write_text(
        "environments:\n"
        "  pilot-src:\n"
        "    display_name: test source\n"
        "    postgres:\n"
        "      dsn_env: BRAVO_TEST_APP_DSN\n"
        "      app_role: app_writer\n"
        "      app_tables: [messages]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAVO_ENVIRONMENTS_FILE", str(reg))
    with TestClient(create_app()) as c:
        yield c


def test_environments_and_snapshot(client, pg_server):
    r = client.get("/v1/environments")
    assert r.status_code == 200
    body = r.json()
    assert body["environments"] == [{"environment_id": "pilot-src", "display_name": "test source",
                                     "sources": ["postgres"]}]
    assert set(body["credential_source"]) == {"label", "temporary_pilot_path"}
    r = client.get("/v1/environments/pilot-src/snapshot", params={"sources": "postgres"})
    assert r.status_code == 200
    body = r.json()
    assert body["environment_id"] == "pilot-src"
    assert set(body["sources"]) == {"postgres"}
    pg = body["sources"]["postgres"]["data"]
    assert pg["app_tables"]["messages"]["public_privileges"]["value"]["SELECT"] is True
    # The DSN and its password never appear in the output.
    ro_password = pg_server.collector_dsn.rsplit("password=", 1)[1]
    assert ro_password not in r.text
    assert pg_server.collector_dsn not in r.text


def test_unknown_environment_and_bad_sources(client):
    assert client.get("/v1/environments/nope/snapshot").status_code == 404
    assert client.get("/v1/environments/pilot-src/snapshot", params={"sources": "ftp"}).status_code == 422


def test_inventory_endpoint(client, tmp_path, monkeypatch):
    monkeypatch.delenv("BRAVO_INVENTORY_ROOT", raising=False)
    assert client.get("/v1/inventory").status_code == 503
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "s.py").write_text('import boto3\nsm = boto3.client("secretsmanager")\n', encoding="utf-8")
    monkeypatch.setenv("BRAVO_INVENTORY_ROOT", str(app_dir))
    r = client.get("/v1/inventory")
    assert r.status_code == 200
    assert any(f["file"] == "s.py" and f["line"] == 2 for f in r.json()["findings"])


def test_registry_rejects_missing_file(monkeypatch):
    monkeypatch.delenv("BRAVO_ENVIRONMENTS_FILE", raising=False)
    with pytest.raises(RuntimeError):
        load_registry()
