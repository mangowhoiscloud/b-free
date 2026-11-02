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


def test_response_with_credential_material_is_withheld(monkeypatch) -> None:
    app = create_app(parse_registry({"environments": {}}))

    @app.get("/leak")
    def leak() -> dict[str, str]:
        return {"oops": SAMPLES["sigv4_signature"]}

    with TestClient(app) as c:
        r = c.get("/leak")
        assert r.status_code == 500 and r.json()["patterns"] == ["sigv4_signature"]
        assert "X-Amz-Signature" not in r.text
        assert c.get("/healthz").status_code == 200


def test_credential_source_label(monkeypatch) -> None:
    app = create_app(parse_registry({"environments": {}}))
    with TestClient(app) as c:
        assert c.get("/v1/environments").json()["credential_source"]["temporary_pilot_path"] is False
    monkeypatch.setenv("BRAVO_CREDENTIAL_SOURCE", "임시 파일럿 경로: ~/.aws 읽기 전용 마운트")
    with TestClient(app) as c:
        cs = c.get("/v1/environments").json()["credential_source"]
        assert cs["temporary_pilot_path"] is True and "임시" in cs["label"]
