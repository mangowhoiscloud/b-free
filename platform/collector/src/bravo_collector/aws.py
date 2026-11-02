"""AWS read-only collection (FR-1). Only Describe/Get/List calls; never GetSecretValue, never a write.

Credentials come from the standard AWS chain (env, shared config/profile, SSO, web identity, IMDS).
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .environments import AwsSection
from .model import Item, failed, observe, ok

_BOTO_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=10)

# Read-only API calls this module makes. Used by the README and the read-only test.
READ_ONLY_CALLS: tuple[str, ...] = (
    "sts:GetCallerIdentity",
    "s3:GetBucketPolicy", "s3:GetBucketPolicyStatus", "s3:GetBucketPublicAccessBlock", "s3:GetEncryptionConfiguration",
    "s3:GetBucketVersioning", "s3:GetAccountPublicAccessBlock",
    "iam:GetRole", "iam:ListAttachedRolePolicies", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListRolePolicies",
    "iam:GetRolePolicy",
    "kms:DescribeKey", "kms:GetKeyPolicy", "kms:GetKeyRotationStatus",
    "secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy",
    "cloudtrail:DescribeTrails", "cloudtrail:GetTrailStatus", "cloudtrail:GetEventSelectors",
    "eks:DescribeCluster", "eks:ListPodIdentityAssociations", "eks:DescribePodIdentityAssociation",
)


def _code(exc: BaseException) -> str | None:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code"))
    return None


def _absent_codes(*codes: str):
    wanted = frozenset(codes)
    return lambda exc: _code(exc) in wanted


class AwsCollector:
    def __init__(self, section: AwsSection, session: boto3.session.Session | None = None) -> None:
        self.section = section
        self.session = session or boto3.session.Session(profile_name=section.profile, region_name=section.region)

    def client(self, name: str):
        return self.session.client(name, region_name=self.section.region, config=_BOTO_CONFIG)

    # --- identity -------------------------------------------------------------------------------------
    def identity(self) -> Item:
        def call() -> dict[str, str]:
            r = self.client("sts").get_caller_identity()
            return {"account": r["Account"], "arn": r["Arn"]}

        return observe(call)

    # --- S3 -------------------------------------------------------------------------------------------
    def s3_bucket(self, bucket: str) -> dict[str, Item]:
        s3 = self.client("s3")
        return {
            "policy": observe(lambda: _json(s3.get_bucket_policy(Bucket=bucket)["Policy"]),
                              absent_on=_absent_codes("NoSuchBucketPolicy")),
            "policy_status": observe(lambda: s3.get_bucket_policy_status(Bucket=bucket)["PolicyStatus"],
                                     absent_on=_absent_codes("NoSuchBucketPolicy")),
            "public_access_block": observe(
                lambda: s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"],
                absent_on=_absent_codes("NoSuchPublicAccessBlockConfiguration"),
            ),
            "encryption": observe(
                lambda: s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"],
                absent_on=_absent_codes("ServerSideEncryptionConfigurationNotFoundError"),
            ),
            "versioning": observe(lambda: _strip_meta(s3.get_bucket_versioning(Bucket=bucket))),
        }

    def account_public_access_block(self, account: str) -> Item:
        s3c = self.client("s3control")
        return observe(
            lambda: s3c.get_public_access_block(AccountId=account)["PublicAccessBlockConfiguration"],
            absent_on=_absent_codes("NoSuchPublicAccessBlockConfiguration"),
        )

    # --- IAM ------------------------------------------------------------------------------------------
    def iam_role(self, role_name: str) -> dict[str, Item]:
        iam = self.client("iam")

        def role() -> dict[str, Any]:
            r = iam.get_role(RoleName=role_name)["Role"]
            return {
                "arn": r["Arn"],
                "trust_policy": r.get("AssumeRolePolicyDocument"),
                "permissions_boundary": (r.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn"),
                "max_session_duration": r.get("MaxSessionDuration"),
            }

        def attached() -> list[dict[str, Any]]:
            out = []
            for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role_name):
                for p in page["AttachedPolicies"]:
                    pol = iam.get_policy(PolicyArn=p["PolicyArn"])["Policy"]
                    ver = iam.get_policy_version(PolicyArn=p["PolicyArn"], VersionId=pol["DefaultVersionId"])
                    out.append({"arn": p["PolicyArn"], "name": p["PolicyName"],
                                "document": ver["PolicyVersion"]["Document"]})
            return sorted(out, key=lambda x: x["arn"])

        def inline() -> list[dict[str, Any]]:
            out = []
            for page in iam.get_paginator("list_role_policies").paginate(RoleName=role_name):
                for name in page["PolicyNames"]:
                    doc = iam.get_role_policy(RoleName=role_name, PolicyName=name)["PolicyDocument"]
                    out.append({"name": name, "document": doc})
            return sorted(out, key=lambda x: x["name"])

        return {"role": observe(role), "attached_policies": observe(attached), "inline_policies": observe(inline)}

    # --- KMS ------------------------------------------------------------------------------------------
    def kms_key(self, key_id: str) -> dict[str, Item]:
        kms = self.client("kms")
        desc = observe(lambda: kms.describe_key(KeyId=key_id)["KeyMetadata"])
        out: dict[str, Item] = {"metadata": _pick(desc, ("KeyId", "Arn", "KeyManager", "KeyState", "KeySpec",
                                                         "KeyUsage", "Origin", "MultiRegion", "Enabled"))}
        if desc.status != "ok":
            out["policy"] = desc
            out["rotation_enabled"] = desc
            return out
        real_id = desc.value["KeyId"]
        out["policy"] = observe(lambda: _json(kms.get_key_policy(KeyId=real_id, PolicyName="default")["Policy"]))
        if desc.value.get("KeyManager") == "CUSTOMER" and desc.value.get("KeySpec") == "SYMMETRIC_DEFAULT":
            out["rotation_enabled"] = observe(lambda: kms.get_key_rotation_status(KeyId=real_id)["KeyRotationEnabled"])
        else:
            out["rotation_enabled"] = failed("not applicable: rotation status exists only for symmetric customer keys")
        return out

    # --- Secrets Manager (metadata only) --------------------------------------------------------------
    def secret(self, secret_id: str) -> dict[str, Item]:
        sm = self.client("secretsmanager")

        def describe() -> dict[str, Any]:
            r = sm.describe_secret(SecretId=secret_id)
            keep = ("ARN", "Name", "KmsKeyId", "RotationEnabled", "RotationRules", "LastRotatedDate",
                    "LastChangedDate", "NextRotationDate", "PrimaryRegion", "DeletedDate")
            return {k: _iso(r[k]) for k in keep if k in r}

        return {
            "metadata": observe(describe),
            "resource_policy": observe(lambda: _resource_policy(sm, secret_id),
                                       absent_on=lambda exc: isinstance(exc, _NoPolicy)),
        }

    # --- CloudTrail -----------------------------------------------------------------------------------
    def cloudtrail(self) -> Item:
        ct = self.client("cloudtrail")

        def call() -> list[dict[str, Any]]:
            names = list(self.section.cloudtrail_trails)
            trails = ct.describe_trails(trailNameList=names, includeShadowTrails=False)["trailList"]
            out = []
            for t in sorted(trails, key=lambda x: x["TrailARN"]):
                entry: dict[str, Any] = {
                    "arn": t["TrailARN"],
                    "name": t["Name"],
                    "home_region": t.get("HomeRegion"),
                    "multi_region": t.get("IsMultiRegionTrail"),
                    "organization": t.get("IsOrganizationTrail"),
                    "log_file_validation": t.get("LogFileValidationEnabled"),
                    "kms_key_id": t.get("KmsKeyId"),
                    "s3_bucket": t.get("S3BucketName"),
                }
                entry["status"] = observe(lambda arn=t["TrailARN"]: _pick_dict(
                    ct.get_trail_status(Name=arn), ("IsLogging", "LatestDeliveryTime", "LatestDeliveryError")))
                entry["event_selectors"] = observe(lambda arn=t["TrailARN"]: _strip_meta(
                    ct.get_event_selectors(TrailName=arn)))
                out.append(entry)
            return out

        return observe(call)

    # --- EKS ------------------------------------------------------------------------------------------
    def eks_cluster(self, name: str) -> dict[str, Item]:
        eks = self.client("eks")

        def describe() -> dict[str, Any]:
            c = eks.describe_cluster(name=name)["cluster"]
            vpc = c.get("resourcesVpcConfig", {})
            return {
                "arn": c.get("arn"),
                "version": c.get("version"),
                "status": c.get("status"),
                "endpoint_public_access": vpc.get("endpointPublicAccess"),
                "endpoint_private_access": vpc.get("endpointPrivateAccess"),
                "public_access_cidrs": sorted(vpc.get("publicAccessCidrs") or []),
                "logging": c.get("logging", {}).get("clusterLogging"),
                "secrets_encryption": [
                    {"resources": e.get("resources"), "key_arn": (e.get("provider") or {}).get("keyArn")}
                    for e in c.get("encryptionConfig") or []
                ],
                "authentication_mode": (c.get("accessConfig") or {}).get("authenticationMode"),
            }

        def pod_identities() -> list[dict[str, Any]]:
            out = []
            for ns in self.section.app_namespaces:
                for page in eks.get_paginator("list_pod_identity_associations").paginate(clusterName=name,
                                                                                         namespace=ns):
                    for a in page["associations"]:
                        d = eks.describe_pod_identity_association(clusterName=name,
                                                                  associationId=a["associationId"])["association"]
                        out.append({"namespace": d["namespace"], "service_account": d["serviceAccount"],
                                    "role_arn": d["roleArn"]})
            return sorted(out, key=lambda x: (x["namespace"], x["service_account"], x["role_arn"]))

        return {"cluster": observe(describe), "pod_identity_associations": observe(pod_identities)}

    # --- all ------------------------------------------------------------------------------------------
    def collect(self) -> dict[str, Any]:
        sec = self.section
        ident = self.identity()
        data: dict[str, Any] = {"region": sec.region, "caller": ident}
        if sec.account_public_access_block:
            data["account_public_access_block"] = (
                self.account_public_access_block(ident.value["account"]) if ident.status == "ok" else ident
            )
        data["s3_buckets"] = {b: self.s3_bucket(b) for b in sorted(sec.s3_buckets)}
        data["eks_clusters"] = {c: self.eks_cluster(c) for c in sorted(sec.eks_clusters)}
        roles = set(sec.iam_role_names)
        for cluster in data["eks_clusters"].values():
            assoc = cluster["pod_identity_associations"]
            if assoc.status == "ok":
                roles.update(a["role_arn"].rsplit("/", 1)[-1] for a in assoc.value)
        data["iam_roles"] = {r: self.iam_role(r) for r in sorted(roles)}
        data["kms_keys"] = {k: self.kms_key(k) for k in sorted(sec.kms_key_ids)}
        data["secrets"] = {s: self.secret(s) for s in sorted(sec.secret_ids)}
        data["cloudtrail"] = self.cloudtrail()
        return data


def _resource_policy(sm: Any, secret_id: str) -> Any:
    r = sm.get_resource_policy(SecretId=secret_id)
    pol = r.get("ResourcePolicy")
    if pol is None:
        raise _NoPolicy("no resource policy")
    return _json(pol)


class _NoPolicy(Exception):
    pass


def _json(text: str) -> Any:
    import json

    return json.loads(text)


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _strip_meta(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _iso(v) for k, v in d.items() if k != "ResponseMetadata"}


def _pick_dict(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: _iso(d[k]) for k in keys if k in d}


def _pick(item: Item, keys: tuple[str, ...]) -> Item:
    if item.status != "ok":
        return item
    return ok(_pick_dict(item.value, keys))
