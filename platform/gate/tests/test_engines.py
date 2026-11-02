"""Probes against real engines (no mocks): postgres:16 x2, a real RGW or S3 bucket named by BRAVO_TEST_S3_*,
the HTTP API, and read-only AWS."""

from __future__ import annotations

import boto3
import pytest
from fastapi.testclient import TestClient

from bravo_gate.app import create_app
from bravo_gate.environments import parse_registry
from bravo_gate.probes.objstore import ObjectStoreProbe
from bravo_gate.service import compare


def _pg_registry(plain, tls) -> dict:
    return {"environments": {
        "src": {"postgres": {"dsn_env": "T_SRC_DSN", "app_role": "app_writer", "app_tables": ["messages"]}},
        "dst": {"postgres": {"dsn_env": "T_DST_DSN", "app_role": "app_writer", "app_tables": ["messages"],
                             "tls": {"sslrootcert_env": "T_DST_CA"}}},
    }}


@pytest.fixture
def pg_env(pg_pair, monkeypatch: pytest.MonkeyPatch):
    plain, tls = pg_pair
    monkeypatch.setenv("T_SRC_DSN", plain.probe_dsn)
    monkeypatch.setenv("T_DST_DSN", tls.probe_dsn)
    monkeypatch.setenv("T_DST_CA", tls.ca_file)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.delenv("PGPASSFILE", raising=False)
    return parse_registry(_pg_registry(plain, tls))


def _q(report: dict) -> dict[str, dict]:
    return {r["control_id"]: r for r in report["questions"]}


@pytest.mark.pg
def test_postgres_controls_plain_vs_tls(pg_env) -> None:
    r = compare(pg_env.get("src"), pg_env.get("dst"))
    q = _q(r)
    # ENC-01: plaintext server fails, TLS-only server with CA verify-full passes.
    assert q["DB-TLS"]["src"] == "FAIL"
    assert "plaintext_accepted" in q["DB-TLS"]["src_evidence"]["reason"]
    assert q["DB-TLS"]["dst"] == "PASS", q["DB-TLS"]["dst_evidence"]
    # IAM-01: scram everywhere, real no-password login refused on both.
    assert q["DB-AUTH"]["src"] == "PASS" and q["DB-AUTH"]["dst"] == "PASS", (q["DB-AUTH"]["src_evidence"],
                                                                          q["DB-AUTH"]["dst_evidence"])
    assert q["DB-ANON-READ"]["src"] == "PASS" and q["DB-ANON-READ"]["dst"] == "PASS"
    # AUD-01 and DR-01: stock images have no pgaudit and no archiving -> failure on both, not "equivalent".
    assert q["DB-AUDIT"]["verdict"] == "BLOCK" and q["DB-AUDIT"]["reason"] == "fail_both"
    assert q["DR-ARCHIVE"]["reason"] == "fail_both"
    assert r["gate_verdict"] == "BLOCK"


@pytest.mark.pg
def test_planted_public_grant_is_a_regression(pg_pair, pg_env) -> None:
    import psycopg

    _plain, tls = pg_pair
    with psycopg.connect(tls.superuser_dsn, autocommit=True) as conn:
        conn.execute("GRANT SELECT ON messages TO PUBLIC")
    try:
        q = _q(compare(pg_env.get("src"), pg_env.get("dst")))
        assert q["DB-ANON-READ"]["src"] == "PASS" and q["DB-ANON-READ"]["dst"] == "FAIL"
        assert q["DB-ANON-READ"]["reason"] == "regression"
    finally:
        with psycopg.connect(tls.superuser_dsn, autocommit=True) as conn:
            conn.execute("REVOKE SELECT ON messages FROM PUBLIC")


@pytest.mark.pg
def test_same_environment_twice_gives_identical_verdicts(pg_env) -> None:
    a = compare(pg_env.get("src"), pg_env.get("dst"))
    b = compare(pg_env.get("src"), pg_env.get("dst"))
    assert a["input_digest"] == b["input_digest"]
    assert a["questions"] == b["questions"] and a["controls"] == b["controls"]


@pytest.mark.pg
def test_probe_role_cannot_write(pg_pair) -> None:
    import psycopg

    plain, _ = pg_pair
    with psycopg.connect(plain.probe_dsn, autocommit=True) as conn, pytest.raises(psycopg.Error):
        conn.execute("INSERT INTO messages (body) VALUES ('x')")


@pytest.mark.pg
def test_http_api_contract(pg_env) -> None:
    client = TestClient(create_app(pg_env))
    rows = client.get("/v1/controls", params={"source": "src", "target": "dst"}).json()
    assert rows and all(set(r) == {"control_id", "question", "src", "dst"} for r in rows)
    ids = [r["control_id"] for r in rows]
    assert ids == ["DB-ANON-READ", "DB-AUTH", "DB-TLS", "DB-AUDIT", "DR-ARCHIVE"]
    ctl = client.get("/v1/controls", params={"source": "src", "target": "dst", "level": "control"}).json()
    assert [r["control_id"] for r in ctl] == ["SEC-01", "ENC-01", "AUD-01", "NET-01", "IAM-01", "DR-01"]
    rep = client.get("/v1/controls/report", params={"source": "src", "target": "dst"}).json()
    assert rep["gate_verdict"] == "BLOCK" and rep["expires_at"] > rep["collected_at"]
    assert client.get("/v1/controls", params={"source": "src", "target": "nope"}).status_code == 404
    assert client.get("/v1/controls", params={"source": "src"}).status_code == 422


# --- object store: a real RGW or S3 bucket named by BRAVO_TEST_S3_* (no local stand-in, D-22) ----------------
def _section(target, *, canary_key: str | None = None) -> dict:
    sec: dict = {"bucket": target.bucket, "endpoint_url": target.endpoint, "region": target.region,
                 "addressing_style": target.addressing_style, "canary_key": canary_key or target.canary_key,
                 "write_probes": target.write_probes}
    if target.app_env:
        sec["app_credential"] = {"access_key_env": target.app_env[0], "secret_key_env": target.app_env[1]}
    else:
        sec["app_credential"] = {"default_chain": True}
    if target.other_env:
        sec["other_credential"] = {"access_key_env": target.other_env[0], "secret_key_env": target.other_env[1]}
    return sec


def _probe(target, **kw) -> ObjectStoreProbe:
    return ObjectStoreProbe(parse_registry({"environments": {"e": {"object_store": _section(target, **kw)}}})
                            .get("e").object_store)


@pytest.mark.s3
def test_object_store_behaviour_on_real_store(s3_target) -> None:
    p = _probe(s3_target)
    results = {"OBJ-01": p.obj01(), "OBJ-02": p.obj02(), "OBJ-03": p.obj03(), "OBJ-04": p.obj04(),
               "OBJ-05": p.obj05(), "OBJ-06": p.obj06(), "OBJ-ENC": p.obj_enc()}
    for qid, o in results.items():
        assert o.verdict in {"PASS", "FAIL", "UNKNOWN"} and o.reason, (qid, o)
    canary = p.canary()
    if canary.kind != "ok":  # without the platform's canary a denial is not proof: never a pass
        for qid in ("OBJ-01", "OBJ-05", "OBJ-06"):
            assert results[qid].verdict != "PASS", (qid, results[qid])
    if not s3_target.write_probes:
        assert results["OBJ-03"].reason == "write_probe_disabled"
    if s3_target.other_env is None:
        assert results["OBJ-05"].reason == "other_credential_missing"
    if s3_target.endpoint.startswith("http://"):
        assert results["OBJ-06"].verdict == "FAIL" and results["OBJ-06"].reason == "endpoint_is_plain_http"


@pytest.mark.s3
def test_missing_canary_is_never_a_pass(s3_target) -> None:
    p = _probe(s3_target, canary_key="_gate/bravo-gate-test-absent-key")
    assert p.canary().kind in {"missing_key", "denied", "error", "unverified"}
    for o in (p.obj01(), p.obj05(), p.obj06()):
        assert o.verdict != "PASS", o


def test_plain_http_endpoint_fails_obj06_without_network() -> None:
    """The endpoint check needs no store: an app path over http:// is a FAIL by definition."""
    sec = parse_registry({"environments": {"e": {"object_store": {
        "bucket": "attachments", "endpoint_url": "http://rgw.invalid:80", "app_credential": {"default_chain": True},
    }}}}).get("e").object_store
    o = ObjectStoreProbe(sec).obj06()
    assert o.verdict == "FAIL" and o.reason == "endpoint_is_plain_http"


def test_unconfigured_credentials_are_unknown_without_network() -> None:
    sec = parse_registry({"environments": {"e": {"object_store": {"bucket": "attachments"}}}}).get("e").object_store
    p = ObjectStoreProbe(sec)
    assert p.obj04().reason == "app_credential_missing"
    assert p.obj05().reason == "other_credential_missing"
    assert p.obj03().reason == "write_probe_disabled"


# --- real AWS, read-only --------------------------------------------------------------------------------------
def _aws_bucket() -> tuple[str, str] | None:
    try:
        sess = boto3.session.Session()
        if sess.get_credentials() is None:
            return None
        sess.client("sts").get_caller_identity()
        buckets = sorted(b["Name"] for b in sess.client("s3").list_buckets().get("Buckets", []))
        if not buckets:
            return None
        loc = sess.client("s3").get_bucket_location(Bucket=buckets[0]).get("LocationConstraint") or "us-east-1"
        return buckets[0], loc
    except Exception:  # noqa: BLE001
        return None


@pytest.mark.aws
def test_aws_s3_read_only_probes() -> None:
    found = _aws_bucket()
    if found is None:
        pytest.skip("no AWS credentials in the default chain, or no bucket visible to them")
    bucket, region = found
    sec = parse_registry({"environments": {"aws": {"object_store": {
        "bucket": bucket, "region": region, "app_credential": {"default_chain": True}, "write_probes": False,
    }}}}).get("aws").object_store
    p = ObjectStoreProbe(sec)
    o2 = p.obj02()
    assert o2.verdict in {"PASS", "FAIL"}, o2  # the unsigned LIST got an answer from S3
    # The platform has not created the canary on AWS (needs approval): never a pass.
    o1 = p.obj01()
    assert o1.verdict in {"UNKNOWN", "FAIL"}
    if o1.verdict == "UNKNOWN":
        assert o1.reason in {"canary_missing", "canary_unverified"}
    assert p.obj03().reason == "write_probe_disabled"
    enc = p.obj_enc()  # read-only GetBucketEncryption (+ DescribeKey for aws:kms)
    assert enc.verdict in {"PASS", "FAIL", "UNKNOWN"} and enc.reason
