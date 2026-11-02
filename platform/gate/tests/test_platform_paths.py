"""The gate's own read paths on the platform: EKS token over a D-16 link, a DB dialled through a link with
verify-full against the DB's own name, and OBJ-06's plain request to a store reached through a link."""

from __future__ import annotations

import base64
import urllib.parse

import boto3
import pytest
from psycopg.conninfo import conninfo_to_dict

from bravo_gate.environments import KubernetesSection, ObjectStoreSection, PostgresSection
from bravo_gate.probes import kube_auth
from bravo_gate.probes.objstore import _endpoint
from bravo_gate.probes.postgres import PostgresProbe


def test_eks_token_is_a_presigned_get_caller_identity_bound_to_the_cluster() -> None:
    session = boto3.session.Session(aws_access_key_id="AKIAEXAMPLE", aws_secret_access_key="example",  # noqa: S106
                                    region_name="ap-northeast-2")
    token = kube_auth.eks_token(session, "bravo-source", "ap-northeast-2")
    assert token.startswith("k8s-aws-v1.")
    raw = token.removeprefix("k8s-aws-v1.")
    url = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://sts.ap-northeast-2.amazonaws.com/")
    assert q["Action"] == ["GetCallerIdentity"]
    assert "x-k8s-aws-id" in q["X-Amz-SignedHeaders"][0]
    assert q["X-Amz-Expires"] == ["60"]


def test_gke_without_endpoint_is_not_configured() -> None:
    with pytest.raises(kube_auth.NotConfigured):
        kube_auth.api_client(KubernetesSection(provider="gke"))


def test_db_dial_host_keeps_the_tls_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SRC_DSN", "host=chat-db-rw.chat.svc port=5432 dbname=chat user=gate_ro sslmode=verify-full")
    monkeypatch.setattr("bravo_gate.probes.postgres.socket.gethostbyname", lambda h: "10.43.44.114")
    probe = PostgresProbe(PostgresSection(dsn_env="SRC_DSN", dial_host="src-db.bravo-link.svc.cluster.local"))
    d = conninfo_to_dict(probe.dsn() or "")
    assert d["host"] == "chat-db-rw.chat.svc"  # libpq checks the certificate against this name
    assert d["hostaddr"] == "10.43.44.114"  # and connects to the link


def test_db_dial_host_that_does_not_resolve_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SRC_DSN", "host=chat-db-rw.chat.svc dbname=chat user=gate_ro")

    def boom(_h: str) -> str:
        raise OSError("no such host")

    monkeypatch.setattr("bravo_gate.probes.postgres.socket.gethostbyname", boom)
    probe = PostgresProbe(PostgresSection(dsn_env="SRC_DSN", dial_host="missing.bravo-link.svc"))
    obs = probe.archive()
    assert (obs.verdict, obs.reason) == ("UNKNOWN", "db_unreachable")


def test_obj06_plain_request_goes_to_the_plain_listener_when_set() -> None:
    s = ObjectStoreSection(bucket="b", endpoint_url="https://127.0.0.1:18443",
                           plain_http_endpoint_url="http://127.0.0.1:18080")
    assert _endpoint(s, plain_http=True) == "http://127.0.0.1:18080"
    assert _endpoint(ObjectStoreSection(bucket="b", endpoint_url="https://h:18443"), plain_http=True) == "http://h:18443"


def test_gcs_obj_enc_reads_the_json_api_default_kms_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import ClientError

    from bravo_gate.probes import objstore

    objects = {"_gate/canary": {"x-goog-encryption-kms-key-name": "k/cryptoKeyVersions/1"}}

    class FakeS3:
        def get_bucket_encryption(self, Bucket: str) -> dict:  # noqa: N803
            raise ClientError({"Error": {"Code": "NotImplemented"}}, "GetBucketEncryption")

        def get_paginator(self, name: str):  # noqa: ANN201
            class P:
                def paginate(self, **kw):  # noqa: ANN201, ANN003
                    return [{"Contents": [{"Key": k} for k in sorted(objects)]}]
            return P()

        def head_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
            return {"ResponseMetadata": {"HTTPHeaders": objects[Key]}}

    monkeypatch.setenv("GCS_AK", "x")
    monkeypatch.setenv("GCS_SK", "y")
    monkeypatch.setattr(objstore, "_client", lambda *a, **k: FakeS3())
    s = ObjectStoreSection(bucket="b", endpoint_url="https://storage.googleapis.com",
                           app_credential={"access_key_env": "GCS_AK", "secret_key_env": "GCS_SK"})
    key = "projects/p/locations/asia-northeast1/keyRings/r/cryptoKeys/k"
    monkeypatch.setattr(objstore, "gcs_bucket_encryption", lambda b: {"defaultKmsKeyName": key})
    obs = objstore.ObjectStoreProbe(s).obj_enc()
    assert (obs.verdict, obs.reason) == ("PASS", "customer_managed_key")
    assert obs.evidence["object_level"]["state"] == "checked" and obs.evidence["object_level"]["objects"] == 1
    # B13: an object written before the bucket default (no CMEK header) fails the question
    objects["att/old"] = {}
    obs = objstore.ObjectStoreProbe(s).obj_enc()
    assert (obs.verdict, obs.reason) == ("FAIL", "plaintext_objects_present")
    assert obs.evidence["object_level"]["not_customer_key_sample"] == ["att/old"]
    monkeypatch.setattr(objstore, "gcs_bucket_encryption", lambda b: None)
    obs = objstore.ObjectStoreProbe(s).obj_enc()
    assert (obs.verdict, obs.reason) == ("FAIL", "not_customer_key")


def test_secret_transformers_judge_provider_from_apiserver_metrics() -> None:
    from bravo_gate.probes.neutral import judge_secret_transformers, parse_metrics

    text = "\n".join([
        "# HELP apiserver_storage_transformation_operations_total x",
        'apiserver_storage_transformation_operations_total{resource="secrets",status="OK",'
        'transformation_type="to_storage",transformer_prefix="k8s:enc:aescbc:v1:"} 175',
        'apiserver_storage_transformation_operations_total{resource="secrets",status="OK",'
        'transformation_type="from_storage",transformer_prefix="aescbckey:"} 6386',
    ])
    obs = judge_secret_transformers(parse_metrics(text, {"apiserver_storage_transformation_operations_total"}))
    assert (obs.verdict, obs.reason) == ("PASS", "cluster_secrets_encrypted")
    assert obs.evidence["secret_transformers"] == ["aescbc"]  # the RKE2 key-name prefix is not a provider
    ident = 'apiserver_storage_transformation_operations_total{resource="secrets",status="OK",transformer_prefix=""} 3'
    obs = judge_secret_transformers(parse_metrics(ident, {"apiserver_storage_transformation_operations_total"}))
    assert (obs.verdict, obs.reason) == ("FAIL", "cluster_secrets_not_encrypted")


def test_neutral_question_ids_replace_the_aws_only_ones() -> None:
    from bravo_gate.controls import QUESTION_BY_ID

    for old in ("SEC-SECRET-READ-AWS", "SEC-ENC-CMK", "AUD-PLATFORM"):
        assert old not in QUESTION_BY_ID
    for new in ("SEC-SECRET-STORE-READ", "SEC-SECRET-AT-REST", "AUD-CONTROL-PLANE"):
        assert QUESTION_BY_ID[new].needs == "platform"
