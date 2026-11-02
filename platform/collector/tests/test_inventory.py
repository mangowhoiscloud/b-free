"""FR-2 inventory: rule-table classification with file:line evidence, determinism, skips, masking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bravo_collector.inventory import RULES, RULES_VERSION, InventoryReport, Tier, main_inventory, mask, scan

APP_PY = '''\
import os

import boto3


def db_password() -> str:
    sm = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
    resp = sm.get_secret_value(SecretId=os.environ["DB_PASSWORD_SECRET_ID"])
    return resp["SecretString"]


def store():
    return boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL"))


def legacy_store():
    return boto3.client("s3", endpoint_url="https://s3.ap-northeast-2.amazonaws.com")


def dsn() -> str:
    return os.environ["DATABASE_URL"]


def seal(data: bytes) -> bytes:
    kms = boto3.client("kms")
    return kms.encrypt(KeyId="alias/chat-app", Plaintext=data)["CiphertextBlob"]


def queue():
    return boto3.client("sqs")


LEGACY_DSN = "postgresql://app:hunter2@db.prod.internal.corp:5432/chat"
'''

MAIN_TF = '''\
resource "aws_s3_bucket" "attachments" {
  bucket = "chat-attachments"
}

resource "aws_s3_bucket_policy" "attachments" {
  bucket = aws_s3_bucket.attachments.id
}

resource "aws_secretsmanager_secret" "db" {
  name = "chat/db"
}

resource "aws_kms_key" "app" {}

resource "aws_iam_role" "app" {
  name = "chat-app"
}

resource "aws_db_instance" "legacy" {}

resource "aws_sqs_queue" "jobs" {}
'''

UPLOAD_TS = '''\
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import { SNSClient } from "@aws-sdk/client-sns";

export const s3 = new S3Client({ endpoint: "https://storage.example-cloud.io" });
'''

SA_YAML = '''\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: chat-app
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/chat-app
'''

ENV_EXAMPLE = '''\
# comment with postgresql://ignored:ignored@nowhere/db
S3_ENDPOINT_URL=http://rook-ceph-rgw-bravo.rook-ceph.svc:80
DB_DSN=host=pgbouncer port=6432 dbname=chat user=app_writer
'''


@pytest.fixture()
def app_tree(tmp_path: Path) -> Path:
    root = tmp_path / "chat-app"
    (root / "src" / "chat").mkdir(parents=True)
    (root / "src" / "chat" / "app.py").write_text(APP_PY)
    (root / "infra").mkdir()
    (root / "infra" / "main.tf").write_text(MAIN_TF)
    (root / "web").mkdir()
    (root / "web" / "upload.ts").write_text(UPLOAD_TS)
    (root / "k8s").mkdir()
    (root / "k8s" / "sa.yaml").write_text(SA_YAML)
    (root / ".env.example").write_text(ENV_EXAMPLE)
    # Must be skipped.
    (root / ".git").mkdir()
    (root / ".git" / "config.py").write_text('import boto3\nboto3.client("secretsmanager")\n')
    (root / "node_modules" / "aws").mkdir(parents=True)
    (root / "node_modules" / "aws" / "index.js").write_text('import "@aws-sdk/client-kms";\n')
    (root / "blob.bin").write_bytes(b"\0\1\2boto3.client('kms')")
    return root


def _at(report: InventoryReport, file: str, line: int) -> dict[str, str]:
    """rule_id -> tier for findings at file:line (classified and unclassified)."""
    return {f.rule_id: f.tier for f in [*report.findings, *report.unclassified] if f.file == file and f.line == line}


def test_python_sdk_and_dsn_classification(app_tree: Path) -> None:
    r = scan(app_tree)
    py = "src/chat/app.py"
    assert _at(r, py, 7)["PY-SM-CLIENT"] == Tier.CODE_CHANGE
    assert _at(r, py, 8)["PY-SM-CALL"] == Tier.CODE_CHANGE
    assert _at(r, py, 13)["PY-S3-ENDPOINT-CONFIG"] == Tier.CONFIG_SWAP
    assert _at(r, py, 13)["PY-ENV-ENDPOINT"] == Tier.CONFIG_SWAP
    legacy = _at(r, py, 17)
    assert legacy["PY-S3-ENDPOINT-LITERAL"] == Tier.CONFIG_SWAP
    assert legacy["GEN-AWS-ENDPOINT"] == Tier.CONFIG_SWAP
    assert _at(r, py, 21)["PY-ENV-DSN"] == Tier.NO_CHANGE
    assert _at(r, py, 25)["PY-KMS-CLIENT"] == Tier.CODE_CHANGE
    kms_call = _at(r, py, 26)
    assert kms_call["PY-KMS-CALL"] == Tier.CODE_CHANGE
    assert kms_call["GEN-KMS-ARN"] == Tier.ADAPTER  # KeyId="alias/chat-app"
    assert _at(r, py, 30)["PY-AWS-OTHER-CLIENT"] == Tier.UNCLASSIFIED
    assert _at(r, py, 33)["GEN-DSN-URL"] == Tier.CODE_CHANGE  # literal DSN in code (audit 37)

    by_rule = {(f.rule_id, f.line): f for f in r.findings if f.file == py}
    assert by_rule[("PY-S3-ENDPOINT-LITERAL", 17)].hardcoded is True
    assert by_rule[("PY-S3-ENDPOINT-CONFIG", 13)].hardcoded is False
    assert by_rule[("GEN-DSN-URL", 33)].hardcoded is True
    other = next(f for f in r.unclassified if f.rule_id == "PY-AWS-OTHER-CLIENT")
    assert other.service == "aws:sqs"


def test_terraform_typescript_and_config(app_tree: Path) -> None:
    r = scan(app_tree)
    tf = "infra/main.tf"
    assert _at(r, tf, 1)["TF-S3-BUCKET"] == Tier.CONFIG_SWAP
    assert _at(r, tf, 5)["TF-S3-POLICY"] == Tier.ADAPTER
    assert _at(r, tf, 9)["TF-SM"] == Tier.ADAPTER
    assert _at(r, tf, 13)["TF-KMS"] == Tier.ADAPTER
    assert _at(r, tf, 15)["TF-IAM"] == Tier.ADAPTER
    assert _at(r, tf, 19)["TF-RDS"] == Tier.UNCLASSIFIED
    assert _at(r, tf, 21)["TF-AWS-OTHER"] == Tier.UNCLASSIFIED

    ts = "web/upload.ts"
    assert _at(r, ts, 1)["JS-SDK3-S3"] == Tier.CONFIG_SWAP
    assert _at(r, ts, 2)["JS-SDK3-OTHER"] == Tier.UNCLASSIFIED
    endpoint_line = _at(r, ts, 4)
    assert endpoint_line["JS-S3-ENDPOINT-LITERAL"] == Tier.CONFIG_SWAP
    assert endpoint_line["GEN-URL-LITERAL"] == Tier.UNCLASSIFIED  # host not decidable by rule

    sa = _at(r, "k8s/sa.yaml", 6)
    assert sa["CFG-IRSA"] == Tier.ADAPTER
    assert sa["GEN-IAM-ROLE-ARN"] == Tier.ADAPTER

    env = ".env.example"
    assert _at(r, env, 1) == {}  # comment line
    assert _at(r, env, 2) == {"CFG-ENDPOINT-KEY": Tier.CONFIG_SWAP}  # cluster-local host is not a cloud URL
    dsn = _at(r, env, 3)
    assert dsn["CFG-DSN-KEY"] == Tier.CONFIG_SWAP
    assert dsn["GEN-DSN-LIBPQ"] == Tier.CONFIG_SWAP
    libpq = next(f for f in r.findings if f.file == env and f.rule_id == "GEN-DSN-LIBPQ")
    assert libpq.hardcoded is False  # literal in configuration, not in code


def test_skips_vcs_vendor_and_binary(app_tree: Path) -> None:
    r = scan(app_tree)
    files = {f.file for f in [*r.findings, *r.unclassified]}
    assert not any(f.startswith((".git/", "node_modules/")) for f in files)
    assert "blob.bin" not in files
    assert r.files_scanned == 5


def test_deterministic_and_relative(app_tree: Path) -> None:
    a = json.dumps(scan(app_tree).model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    b = json.dumps(scan(app_tree).model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    assert a == b
    report = scan(app_tree)
    assert report.root == "chat-app"
    assert report.rules_version == RULES_VERSION
    assert str(app_tree) not in a
    ids = [f.id for f in report.findings]
    assert len(ids) == len(set(ids))
    assert report.findings == sorted(report.findings, key=lambda f: (f.file, f.line, f.rule_id, f.id))
    # The summary counts dependencies (file, line, tier, service); the lists keep every matching rule.
    hits = {(f.file, f.line, f.tier, f.service) for f in [*report.findings, *report.unclassified]}
    assert sum(report.summary.values()) == len(hits)
    assert set(report.summary) == {t.value for t in Tier}


def test_digest_changes_with_content(app_tree: Path) -> None:
    before = scan(app_tree).input_digest
    (app_tree / "src" / "chat" / "app.py").write_text(APP_PY + "\n# edit\n")
    assert scan(app_tree).input_digest != before


def test_evidence_masks_secrets(app_tree: Path) -> None:
    r = scan(app_tree)
    dumped = json.dumps(r.model_dump(mode="json"), ensure_ascii=False)
    assert "hunter2" not in dumped
    dsn = next(f for f in r.findings if f.rule_id == "GEN-DSN-URL")
    assert "app:***@db.prod.internal.corp" in dsn.evidence
    assert mask("password=s3cr3t host=db") == "password=*** host=db"
    assert mask("key AKIAABCDEFGHIJKLMNOP") == "key AKIA****************"
    assert mask("password=%s") == "password=%s"  # placeholder kept


def test_rule_table_is_data() -> None:
    ids = [r.rule_id for r in RULES]
    assert len(ids) == len(set(ids))
    assert {r.tier for r in RULES} == set(Tier)


def test_cli(app_tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("BRAVO_INVENTORY_ROOT", raising=False)
    assert main_inventory([]) == 2
    assert "BRAVO_INVENTORY_ROOT" in capsys.readouterr().err
    monkeypatch.setenv("BRAVO_INVENTORY_ROOT", str(app_tree))
    assert main_inventory([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == "inventory/v1"
    assert main_inventory(["--path", str(app_tree / "missing")]) == 2


DEMO = Path(__file__).resolve().parents[3] / "apps" / "demo"


@pytest.mark.skipif(not DEMO.is_dir(), reason="apps/demo not present in this checkout")
def test_scan_real_demo_app() -> None:
    report = scan(DEMO)
    assert report.root == "demo"
    for f in [*report.findings, *report.unclassified]:
        path = DEMO / f.file
        assert path.is_file(), f.file
        assert 1 <= f.line <= len(path.read_text(encoding="utf-8").splitlines())


def test_test_code_is_listed_apart_and_summary_counts_dependencies(tmp_path: Path) -> None:
    root = tmp_path / "svc"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "a.py").write_text('import boto3\nsm = boto3.client("secretsmanager")\n')
    (root / "tests" / "test_a.py").write_text('DSN = "postgresql://u@db:5432/x"\n')
    r = scan(root)
    assert all(not f.file.startswith("tests/") for f in [*r.findings, *r.unclassified])
    assert [f.file for f in r.test_findings] == ["tests/test_a.py"]
    assert sum(r.test_summary.values()) == 1
    assert r.summary[Tier.CODE_CHANGE] == 1


def test_trailing_yaml_comment_is_not_evidence(tmp_path: Path) -> None:
    root = tmp_path / "chart"
    root.mkdir()
    (root / "values.yaml").write_text(
        'endpointUrl: ""   # e.g. https://storage.googleapis.com\n'
        'annotations: {}   # eks.amazonaws.com/role-arn goes here\n'
        'url: "https://example.com/#frag"\n')
    r = scan(root)
    assert [f.line for f in [*r.findings, *r.unclassified] if f.line in (1, 2)] == []



def test_masks_cover_secret_shapes() -> None:
    """Audit B18: evidence never carries these values."""
    from bravo_collector.inventory.detector import mask

    cases = {
        'aws_secret_access_key=abcdEFGH1234abcdEFGH1234': "abcdEFGH1234",
        "DB_PASSWORD=hunter2-value": "hunter2-value",
        'POSTGRES_PASSWORD: "s3cr3t"': "s3cr3t",
        '{"user": "app", "password": "jsonpass1"}': "jsonpass1",
        "postgresql://app:dsnpass9@db:5432/chat": "dsnpass9",
        "host=db user=app password=libpqpass": "libpqpass",
        "export SESSION_TOKEN=tok-123456": "tok-123456",
    }
    for line, secret in cases.items():
        out = mask(line)
        assert secret not in out, (line, out)
    assert mask("password=%s") == "password=%s"  # templates stay readable


def test_skipped_files_are_listed_with_reason(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    (root / "bin.py").write_bytes(b"\x00\x01binary")
    (root / "latin.py").write_bytes("caf\xe9".encode("latin-1"))
    (root / "ok.py").write_text("x = 1\n")
    r = scan(root)
    assert r.files_skipped == 2
    assert {s["file"]: s["reason"] for s in r.skipped} == {"bin.py": "binary (NUL byte)", "latin.py": "not UTF-8"}
