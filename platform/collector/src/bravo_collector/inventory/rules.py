"""FR-2 rule table: static detection and 3-tier classification (spec §5.1 FR-2, §7.3, 05 §5).

The table is data. Each record names one detection rule and the tier it assigns. Review or
change the tiers here; the detector only applies them. No rule calls an LLM (NFR-5). Items no
rule can place go to ``미분류`` with evidence, for a later AI proposal and a human decision (§8.1).

Tiers
    설정 교체   config swap: the value changes per environment, the code does not (S3 endpoint,
                DSN host, EKS -> K3s/RKE2, CloudTrail -> cluster audit).
    어댑터 추가 adapter / translation: IAM roles, resource and key policies, workload identity,
                secret and key resources that become OpenBao / RGW policies / K8s RBAC.
    코드 수정   code change: the app calls Secrets Manager or KMS directly through an SDK.
    이전 불필요 no change: plain PostgreSQL through a DSN from the environment (§7.3 "그대로").
                Shown apart from the three tiers.
    미분류      unclassified: AWS services or literal hosts outside the table.

``hardcoded`` on a finding is true when a literal value (endpoint, DSN) sits in code
(Python, JS/TS, Go, Java/Kotlin, shell, SQL) instead of configuration (YAML, JSON, .env, Terraform).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

RULES_VERSION = "inventory-rules/2026-11-02.1"


class Tier(StrEnum):
    CONFIG_SWAP = "설정 교체"
    ADAPTER = "어댑터 추가"
    CODE_CHANGE = "코드 수정"
    NO_CHANGE = "이전 불필요"
    UNCLASSIFIED = "미분류"


TIER_CODE: dict[Tier, str] = {
    Tier.CONFIG_SWAP: "config_swap",
    Tier.ADAPTER: "adapter",
    Tier.CODE_CHANGE: "code_change",
    Tier.NO_CHANGE: "no_change",
    Tier.UNCLASSIFIED: "unclassified",
}

# File classes. ``CODE`` classes set ``hardcoded`` on literal findings.
PYTHON = "python"
JS = "js"
GO = "go"
JAVA = "java"
SCRIPT = "script"  # shell, SQL
TERRAFORM = "terraform"
CONFIG = "config"  # YAML, JSON, TOML, INI, .env, Dockerfile, compose
CODE_CLASSES = frozenset({PYTHON, JS, GO, JAVA, SCRIPT})
ALL_CLASSES = frozenset({PYTHON, JS, GO, JAVA, SCRIPT, TERRAFORM, CONFIG})


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    tier: Tier
    service: str  # "*" = taken from the match (e.g. "aws:sqs")
    kind: str
    reason: str
    applies_to: frozenset[str]
    pattern: re.Pattern[str] | None = None  # None: applied by the Python AST pass
    literal: bool = False  # the match is a literal value -> hardcoded when in a code file


def _re(p: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(p, flags)


_SVC_TAIL = r"(?:['\"/]|$)"

RULES: tuple[Rule, ...] = (
    # --- Python, applied on the AST (detector.PythonVisitor) ------------------------------------
    Rule("PY-SM-CLIENT", Tier.CODE_CHANGE, "secretsmanager", "sdk_client",
         "App creates a Secrets Manager client. Target secrets are K8s Secrets from OpenBao + ESO (§7.3).",
         frozenset({PYTHON})),
    Rule("PY-SM-CALL", Tier.CODE_CHANGE, "secretsmanager", "sdk_call",
         "App reads a secret through the Secrets Manager API; replace with a mounted Secret (§7.3, §8.10).",
         frozenset({PYTHON})),
    Rule("PY-KMS-CLIENT", Tier.CODE_CHANGE, "kms", "sdk_client",
         "App creates a KMS client. Target is OpenBao transit (§7.3).", frozenset({PYTHON})),
    Rule("PY-KMS-CALL", Tier.CODE_CHANGE, "kms", "sdk_call",
         "App encrypts/decrypts through the KMS API; replace with OpenBao transit calls (§7.3).",
         frozenset({PYTHON})),
    Rule("PY-S3-ENDPOINT-CONFIG", Tier.CONFIG_SWAP, "s3", "sdk_client",
         "S3 client with endpoint_url from configuration; point it at the S3-compatible target.",
         frozenset({PYTHON})),
    Rule("PY-S3-ENDPOINT-LITERAL", Tier.CONFIG_SWAP, "s3", "sdk_client",
         "S3 client with a literal endpoint_url in code; move it to configuration.",
         frozenset({PYTHON}), literal=True),
    Rule("PY-S3-ENDPOINT-DEFAULT", Tier.CONFIG_SWAP, "s3", "sdk_client",
         "S3 client without endpoint_url; botocore reads AWS_ENDPOINT_URL_S3, so the swap is configuration.",
         frozenset({PYTHON})),
    Rule("PY-AWS-OTHER-CLIENT", Tier.UNCLASSIFIED, "*", "sdk_client",
         "AWS SDK client for a service outside the §7.3 table.", frozenset({PYTHON})),
    Rule("PY-ENV-DSN", Tier.NO_CHANGE, "postgresql", "db_dsn_env",
         "PostgreSQL DSN read from the environment; the app code stays as is (§7.3 DB row).",
         frozenset({PYTHON})),
    Rule("PY-ENV-ENDPOINT", Tier.CONFIG_SWAP, "s3", "endpoint_config",
         "Object-store endpoint read from the environment; set it to the target endpoint.",
         frozenset({PYTHON})),
    # --- JavaScript / TypeScript -------------------------------------------------------------------
    Rule("JS-SDK3-SM", Tier.CODE_CHANGE, "secretsmanager", "sdk_import",
         "Imports the Secrets Manager SDK; target secrets come from OpenBao + ESO.",
         frozenset({JS}), _re(r"@aws-sdk/client-secrets-manager" + _SVC_TAIL)),
    Rule("JS-SDK3-SM-CALL", Tier.CODE_CHANGE, "secretsmanager", "sdk_call",
         "Reads a secret through the Secrets Manager API.",
         frozenset({JS}), _re(r"new\s+(?:GetSecretValue|BatchGetSecretValue)Command\b")),
    Rule("JS-SDK3-KMS", Tier.CODE_CHANGE, "kms", "sdk_import",
         "Imports the KMS SDK; target is OpenBao transit.", frozenset({JS}), _re(r"@aws-sdk/client-kms" + _SVC_TAIL)),
    Rule("JS-SDK3-KMS-CALL", Tier.CODE_CHANGE, "kms", "sdk_call",
         "Encrypts/decrypts through the KMS API.",
         frozenset({JS}), _re(r"new\s+(?:Encrypt|Decrypt|ReEncrypt|GenerateDataKey\w*)Command\b")),
    Rule("JS-SDK3-S3", Tier.CONFIG_SWAP, "s3", "sdk_import",
         "Imports the S3 SDK; the endpoint is a client option or AWS_ENDPOINT_URL_S3.",
         frozenset({JS}), _re(r"@aws-sdk/client-s3" + _SVC_TAIL)),
    Rule("JS-S3-ENDPOINT-LITERAL", Tier.CONFIG_SWAP, "s3", "endpoint_literal",
         "Literal endpoint in the client options; move it to configuration.",
         frozenset({JS}), _re(r"\bendpoint\s*:\s*['\"`]https?://"), literal=True),
    Rule("JS-SDK3-OTHER", Tier.UNCLASSIFIED, "*", "sdk_import",
         "AWS SDK v3 package outside the §7.3 table.",
         frozenset({JS}),
         _re(r"@aws-sdk/(?!client-s3['\"/]|client-secrets-manager['\"/]|client-kms['\"/])([a-z0-9-]+)")),
    Rule("JS-SDK2-SM", Tier.CODE_CHANGE, "secretsmanager", "sdk_client",
         "aws-sdk v2 Secrets Manager client.", frozenset({JS}), _re(r"new\s+AWS\.SecretsManager\b")),
    Rule("JS-SDK2-KMS", Tier.CODE_CHANGE, "kms", "sdk_client",
         "aws-sdk v2 KMS client.", frozenset({JS}), _re(r"new\s+AWS\.KMS\b")),
    Rule("JS-SDK2-S3", Tier.CONFIG_SWAP, "s3", "sdk_client",
         "aws-sdk v2 S3 client; the endpoint is a constructor option.", frozenset({JS}), _re(r"new\s+AWS\.S3\b")),
    Rule("JS-SDK2-OTHER", Tier.UNCLASSIFIED, "*", "sdk_client",
         "aws-sdk v2 client outside the §7.3 table.",
         frozenset({JS}), _re(r"new\s+AWS\.(?!S3\b|SecretsManager\b|KMS\b)([A-Z]\w+)\s*\(")),
    Rule("JS-ENV-DSN", Tier.NO_CHANGE, "postgresql", "db_dsn_env",
         "PostgreSQL DSN read from the environment.",
         frozenset({JS}),
         _re(r"process\.env(?:\.|\[\s*['\"])(DATABASE_URL|[A-Z0-9_]*_DSN|PG(?:HOST|DATABASE|USER|PORT))\b")),
    # --- Go ------------------------------------------------------------------------------------------
    Rule("GO-SDK-SM", Tier.CODE_CHANGE, "secretsmanager", "sdk_import", "Go SDK Secrets Manager package.",
         frozenset({GO}), _re(r"github\.com/aws/aws-sdk-go(?:-v2)?/service/secretsmanager\"")),
    Rule("GO-SDK-KMS", Tier.CODE_CHANGE, "kms", "sdk_import", "Go SDK KMS package.",
         frozenset({GO}), _re(r"github\.com/aws/aws-sdk-go(?:-v2)?/service/kms\"")),
    Rule("GO-SDK-S3", Tier.CONFIG_SWAP, "s3", "sdk_import", "Go SDK S3 package; the endpoint is configuration.",
         frozenset({GO}), _re(r"github\.com/aws/aws-sdk-go(?:-v2)?/service/s3\"")),
    Rule("GO-SDK-OTHER", Tier.UNCLASSIFIED, "*", "sdk_import", "Go SDK package outside the §7.3 table.",
         frozenset({GO}),
         _re(r"github\.com/aws/aws-sdk-go(?:-v2)?/service/(?!s3\"|secretsmanager\"|kms\")([a-z0-9]+)")),
    # --- Java / Kotlin ------------------------------------------------------------------------------
    Rule("JAVA-SDK-SM", Tier.CODE_CHANGE, "secretsmanager", "sdk_import", "Java SDK Secrets Manager package.",
         frozenset({JAVA}), _re(r"(?:software\.amazon\.awssdk\.services|com\.amazonaws\.services)\.secretsmanager\b")),
    Rule("JAVA-SDK-KMS", Tier.CODE_CHANGE, "kms", "sdk_import", "Java SDK KMS package.",
         frozenset({JAVA}), _re(r"(?:software\.amazon\.awssdk\.services|com\.amazonaws\.services)\.kms\b")),
    Rule("JAVA-SDK-S3", Tier.CONFIG_SWAP, "s3", "sdk_import", "Java SDK S3 package; endpointOverride is configuration.",
         frozenset({JAVA}), _re(r"(?:software\.amazon\.awssdk\.services|com\.amazonaws\.services)\.s3\b")),
    Rule("JAVA-SDK-OTHER", Tier.UNCLASSIFIED, "*", "sdk_import", "Java SDK package outside the §7.3 table.",
         frozenset({JAVA}),
         _re(r"(?:software\.amazon\.awssdk\.services|com\.amazonaws\.services)\.(?!s3\b|secretsmanager\b|kms\b)([a-z0-9]+)")),
    # --- Terraform (resource / data blocks; mapped by type in TERRAFORM_TYPES) ----------------------
    Rule("TF-S3-BUCKET", Tier.CONFIG_SWAP, "s3", "iac_resource",
         "Bucket and its settings are re-created on the S3-compatible target store.", frozenset({TERRAFORM})),
    Rule("TF-S3-POLICY", Tier.ADAPTER, "s3", "iac_policy",
         "Bucket policy / public access block / ACL must be translated to the target store policy.",
         frozenset({TERRAFORM})),
    Rule("TF-SM", Tier.ADAPTER, "secretsmanager", "iac_resource",
         "Secret resource becomes an OpenBao path + ExternalSecret mapping.", frozenset({TERRAFORM})),
    Rule("TF-KMS", Tier.ADAPTER, "kms", "iac_resource",
         "KMS key and key policy become an OpenBao transit key and policy.", frozenset({TERRAFORM})),
    Rule("TF-IAM", Tier.ADAPTER, "iam", "iac_policy",
         "IAM roles, policies and workload identity map to K8s RBAC + OpenBao / store policies.",
         frozenset({TERRAFORM})),
    Rule("TF-EKS", Tier.CONFIG_SWAP, "eks", "iac_resource",
         "EKS maps to the landing-zone cluster (K3s/RKE2) by configuration (§7.3).", frozenset({TERRAFORM})),
    Rule("TF-CLOUDTRAIL", Tier.CONFIG_SWAP, "cloudtrail", "iac_resource",
         "CloudTrail maps to cluster audit + pgaudit by configuration (§7.3).", frozenset({TERRAFORM})),
    Rule("TF-RDS", Tier.UNCLASSIFIED, "rds", "iac_resource",
         "Managed RDS is not in the §7.3 table (the table assumes CNPG on both sides).", frozenset({TERRAFORM})),
    Rule("TF-AWS-OTHER", Tier.UNCLASSIFIED, "*", "iac_resource",
         "AWS resource type outside the §7.3 table.", frozenset({TERRAFORM})),
    # --- Kubernetes / config files --------------------------------------------------------------------
    Rule("CFG-IRSA", Tier.ADAPTER, "iam", "workload_identity",
         "IRSA annotation binds a service account to an IAM role; translate to target RBAC / OpenBao role.",
         frozenset({CONFIG, TERRAFORM}), _re(r"eks\.amazonaws\.com/role-arn")),
    Rule("CFG-ESO-AWS", Tier.ADAPTER, "secretsmanager", "secret_store",
         "External Secrets store backed by AWS Secrets Manager; re-point to OpenBao.",
         frozenset({CONFIG}), _re(r"\bservice:\s*['\"]?SecretsManager\b")),
    Rule("CFG-ENDPOINT-KEY", Tier.CONFIG_SWAP, "s3", "endpoint_config",
         "Object-store endpoint setting; set it to the target endpoint.",
         frozenset({CONFIG}), _re(r"\b(AWS_ENDPOINT_URL(?:_S3)?|[A-Z0-9_]*S3_ENDPOINT(?:_URL)?)\b['\"]?\s*[=:]")),
    Rule("CFG-DSN-KEY", Tier.CONFIG_SWAP, "postgresql", "db_endpoint_config",
         "DSN setting; its host changes per environment (PgBouncer target), the code does not.",
         frozenset({CONFIG}), _re(r"\b(DATABASE_URL|[A-Z0-9_]*_DSN)\b['\"]?\s*[=:]")),
    # --- Any text file ------------------------------------------------------------------------------
    Rule("GEN-AWS-ENDPOINT", Tier.CONFIG_SWAP, "*", "endpoint_literal",
         "Literal AWS endpoint host; replace with the target endpoint through configuration.",
         ALL_CLASSES,
         _re(r"(?:https?://[A-Za-z0-9.-]*amazonaws\.com(?:\.cn)?|\b(?:[a-z0-9-]+\.)*"
             r"(?:[a-z0-9-]+\.(?:[a-z]{2}(?:-gov)?-[a-z]+-\d)|s3(?:[.-][a-z0-9-]+)?)\.amazonaws\.com(?:\.cn)?\b)"),
         literal=True),
    Rule("GEN-DSN-URL", Tier.CONFIG_SWAP, "postgresql", "dsn_literal",
         "Literal PostgreSQL URL; the host must come from configuration.",
         ALL_CLASSES, _re(r"\bpostgres(?:ql)?(?:\+\w+)?://[^\s'\"`]+"), literal=True),
    Rule("GEN-DSN-LIBPQ", Tier.CONFIG_SWAP, "postgresql", "dsn_literal",
         "Literal libpq connection string; the host must come from configuration.",
         ALL_CLASSES, _re(r"\bhost=(?![%${<])[^\s'\"]+[^\n]*\bdbname=|\bdbname=[^\n]*\bhost=(?![%${<])[^\s'\"]+"),
         literal=True),
    Rule("GEN-DSN-TEMPLATE", Tier.NO_CHANGE, "postgresql", "db_dsn_template",
         "DSN assembled from variables; the host comes from configuration.",
         ALL_CLASSES,
         _re(r"\bhost=[%${<][^\n]*\bdbname=|\bdbname=[^\n]*\bhost=[%${<]|\bpostgres(?:ql)?://[^\s'\"`]*[{$%]")),
    Rule("GEN-SM-ARN", Tier.ADAPTER, "secretsmanager", "resource_ref",
         "Secrets Manager ARN; the secret is re-created in OpenBao and mapped by ESO.",
         ALL_CLASSES, _re(r"arn:aws(?:-[a-z]+)*:secretsmanager:[a-z0-9-]+:\d{12}:secret:[^\s'\"]+"), literal=True),
    Rule("GEN-KMS-ARN", Tier.ADAPTER, "kms", "resource_ref",
         "KMS key or alias reference; key access must be translated to an OpenBao transit policy.",
         ALL_CLASSES,
         _re(r"arn:aws(?:-[a-z]+)*:kms:[a-z0-9-]+:\d{12}:(?:key|alias)/[^\s'\"]+"
             r"|^(?=(?i:.*(?:kms|keyid|key_id))).*?['\"]alias/[A-Za-z0-9/_-]+['\"]"),
         literal=True),
    Rule("GEN-IAM-ROLE-ARN", Tier.ADAPTER, "iam", "resource_ref",
         "IAM role reference; permissions map to K8s RBAC + OpenBao / store policies.",
         ALL_CLASSES, _re(r"arn:aws(?:-[a-z]+)*:iam::\d{12}:role/[\w+=,.@/-]+"), literal=True),
    Rule("GEN-URL-LITERAL", Tier.UNCLASSIFIED, "*", "endpoint_literal",
         "Literal URL host that no rule places; cloud dependency not decidable by rule.",
         CODE_CLASSES | frozenset({CONFIG}), _re(r"\bhttps?://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)(?::\d+)?"),
         literal=True),
)

RULES_BY_ID: dict[str, Rule] = {r.rule_id: r for r in RULES}

# Terraform resource/data type -> rule. Checked in order: exact names, then prefixes.
TERRAFORM_EXACT: dict[str, str] = {
    "aws_s3_bucket_policy": "TF-S3-POLICY",
    "aws_s3_bucket_public_access_block": "TF-S3-POLICY",
    "aws_s3_account_public_access_block": "TF-S3-POLICY",
    "aws_s3_bucket_acl": "TF-S3-POLICY",
    "aws_eks_pod_identity_association": "TF-IAM",
    "aws_eks_cluster": "TF-EKS",
    "aws_eks_node_group": "TF-EKS",
    "aws_eks_addon": "TF-EKS",
    "aws_cloudtrail": "TF-CLOUDTRAIL",
    "aws_db_instance": "TF-RDS",
    "aws_rds_cluster": "TF-RDS",
    "aws_rds_cluster_instance": "TF-RDS",
}
TERRAFORM_PREFIX: tuple[tuple[str, str], ...] = (
    ("aws_s3_", "TF-S3-BUCKET"),
    ("aws_secretsmanager_", "TF-SM"),
    ("aws_kms_", "TF-KMS"),
    ("aws_iam_", "TF-IAM"),
)
TERRAFORM_BLOCK = re.compile(r"^\s*(resource|data)\s+\"(aws_[a-z0-9_]+)\"\s+\"([^\"]+)\"")

# Python AST helpers.
SM_CALLS = frozenset({
    "get_secret_value", "batch_get_secret_value", "describe_secret", "put_secret_value",
    "create_secret", "update_secret", "rotate_secret", "list_secrets",
})
SM_UNIQUE_CALLS = frozenset({"get_secret_value", "batch_get_secret_value", "put_secret_value", "rotate_secret"})
KMS_CALLS = frozenset({
    "encrypt", "decrypt", "re_encrypt", "generate_data_key", "generate_data_key_without_plaintext",
    "generate_data_key_pair", "sign", "verify", "generate_mac", "verify_mac", "describe_key", "get_public_key",
})
KMS_UNIQUE_CALLS = frozenset({"generate_data_key", "generate_data_key_without_plaintext", "generate_data_key_pair",
                              "re_encrypt"})
CLIENT_FACTORIES = frozenset({"client", "resource", "create_client"})
AWS_SERVICE_NAMES = frozenset({
    "s3", "s3control", "secretsmanager", "kms", "sts", "iam", "sqs", "sns", "dynamodb", "ssm", "ec2", "rds",
    "lambda", "eks", "ecr", "cloudtrail", "cloudwatch", "logs", "events", "kinesis", "firehose", "ses", "sesv2",
    "route53", "elbv2", "acm", "cognito-idp", "stepfunctions", "athena", "glue", "elasticache", "opensearch",
    "bedrock-runtime", "bedrock", "appconfig", "rds-data", "textract", "comprehend", "translate", "polly",
})
ENV_DSN_NAME = re.compile(
    r"^(?:DATABASE_URL|[A-Z0-9_]*_DSN|[A-Z0-9_]*DATABASE_URL|PG(?:HOST|DATABASE|USER|PORT|SERVICE))$"
)
ENV_ENDPOINT_NAME = re.compile(r"^(?:AWS_ENDPOINT_URL(?:_S3)?|[A-Z0-9_]*S3_ENDPOINT(?:_URL)?)$")

# Hosts that are not cloud dependencies: loopback, cluster-local names, reserved test domains.
NON_CLOUD_HOST = re.compile(
    r"^(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|.*\.svc(?:\.cluster\.local)?|.*\.cluster\.local"
    r"|.*\.(?:test|example|invalid|localhost|local|internal)|(?:.*\.)?example\.(?:com|org|net))$",
    re.IGNORECASE,
)
# Specification / namespace URLs that never name an endpoint.
SPEC_HOST = re.compile(
    r"^(?:www\.w3\.org|json-schema\.org|schemas\.xmlsoap\.org|xmlns\.com|purl\.org|opensource\.org"
    r"|www\.apache\.org|semver\.org|keepachangelog\.com|spdx\.org)$",
    re.IGNORECASE,
)
