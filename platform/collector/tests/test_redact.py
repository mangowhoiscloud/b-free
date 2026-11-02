"""Credential guard: errors, logs and HTTP responses never carry credential material (NFR-6, D-26).

The inputs are credential-shaped test strings, not real credentials.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from bravo_collector.app import create_app
from bravo_collector.environments import parse_registry
from bravo_collector.model import failed
from bravo_collector.redact import find_credentials, install_log_filter, redact

SAMPLES = {
    "aws_access_key_id": "key AKIA" + "ABCDEFGHIJKLMNOP" + " used",
    "aws_secret_access_key": "aws_secret_access_key = " + "x" * 40,
    "eks_bearer_token": "token k8s-aws-v1." + "aHR0cHM6Ly9zdHMuYW1hem9uYXdzLmNvbS8" * 2,
    "sigv4_signature": "https://sts.amazonaws.com/?X-Amz-Signature=" + "f" * 64,
    "bearer_header": "Authorization: Bearer " + "eyJhbGciOiJSUzI1NiJ9" * 2,
    "password_kv": "host=db password=" + "s3cr3t-value",
}


def test_every_pattern_is_found_and_redacted() -> None:
    for name, text in SAMPLES.items():
        assert name in find_credentials(text), name
        assert find_credentials(redact(text)) == [], (name, redact(text))


def test_error_items_are_redacted() -> None:
    item = failed(RuntimeError(SAMPLES["eks_bearer_token"]))
    assert "k8s-aws-v1." not in (item.error or "") and "<redacted:eks_bearer_token>" in (item.error or "")


def test_log_records_are_redacted(caplog) -> None:
    install_log_filter()
    logger = logging.getLogger("bravo_collector.test")
    logger.addFilter(logging.getLogger("").filters[-1])
    with caplog.at_level(logging.INFO):
        logger.info("calling with %s", SAMPLES["aws_access_key_id"])
    assert "AKIA" not in caplog.text and "<redacted:aws_access_key_id>" in caplog.text
    assert logging.getLogger("botocore").level == logging.WARNING


def test_response_is_masked_per_field_never_blocked() -> None:
    app = create_app(parse_registry({"environments": {}}))

    @app.get("/leak")
    def leak() -> dict[str, object]:
        return {"oops": SAMPLES["sigv4_signature"], "ok": "plain", "list": [SAMPLES["password_kv"]],
                "masked_already": "password=***"}

    with TestClient(app) as c:
        r = c.get("/leak")
        assert r.status_code == 200
        body = r.json()
        assert "X-Amz-Signature=fff" not in r.text and "s3cr3t-value" not in r.text
        assert body["ok"] == "plain" and body["masked_already"] == "password=***"
        assert body["list"] == ["host=db <redacted:password_kv>"]
        assert r.headers["x-bravo-credential-guard"] == "masked=2;withheld=0"
        assert c.get("/healthz").status_code == 200


def test_masked_values_are_not_credentials() -> None:
    assert find_credentials("password=***") == []
    assert find_credentials("postgresql://a:***@x/chat") == []
    assert find_credentials("aws_secret_access_key = <redacted:aws_secret_access_key>") == []


def test_demo_fixture_does_not_block_inventory(tmp_path, monkeypatch) -> None:
    """The exact fixture from apps/demo/tests/test_unit.py:84 stays a finding with file:line, masked."""
    root = tmp_path / "demo"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text('import boto3\nsm = boto3.client("secretsmanager")\n')
    lines = ["import pytest", ""] + ["# filler"] * 81 + ['    "host=x dbname=chat user=a password=secret",']
    (root / "tests" / "test_unit.py").write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("BRAVO_INVENTORY_ROOT", str(root))
    with TestClient(create_app(parse_registry({"environments": {}}))) as c:
        r = c.get("/v1/inventory")
        assert r.status_code == 200, r.text
        inv = r.json()
        fixture = [f for f in inv["test_findings"] if f["file"] == "tests/test_unit.py"]
        assert fixture and all(f["line"] == 84 for f in fixture)
        assert all("password=***" in f["evidence"] and "secret" not in f["evidence"] for f in fixture)
        assert "password=secret" not in r.text
        snap = c.get("/v1/inventory/snapshot")
        assert snap.headers["x-bravo-inventory-digest"] == inv["input_digest"]
def test_credential_source_label(monkeypatch) -> None:
    app = create_app(parse_registry({"environments": {}}))
    with TestClient(app) as c:
        assert c.get("/v1/environments").json()["credential_source"]["temporary_pilot_path"] is False
    monkeypatch.setenv("BRAVO_CREDENTIAL_SOURCE", "임시 파일럿 경로: ~/.aws 읽기 전용 마운트")
    with TestClient(app) as c:
        cs = c.get("/v1/environments").json()["credential_source"]
        assert cs["temporary_pilot_path"] is True and "임시" in cs["label"]
