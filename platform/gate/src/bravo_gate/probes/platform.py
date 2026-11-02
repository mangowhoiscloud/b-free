"""Kubernetes, AWS and network probes. Kubernetes and AWS calls are reads or review APIs that persist nothing
(SubjectAccessReview, iam:SimulatePrincipalPolicy). Network probes open and close one TCP connection.

SEC-SECRET-READ   another identity cannot read the app secret: K8s SubjectAccessReview and/or IAM simulation (SEC-01)
SEC-ENC-CMK       app secrets are encrypted with a customer-managed key (Secrets Manager KmsKeyId)       (ENC-02)
AUD-PLATFORM      control-plane audit is on: CloudTrail logging (+ EKS audit log type)                    (AUD-01)
NET-DEFAULT-DENY  the app namespace has default-deny ingress and egress NetworkPolicies               (NET-01)
NET-PATHS         control paths connect, denied paths do not                                          (NET-01)
DR-RESTORE        a CNPG Cluster bootstrapped by recovery is healthy                                   (DR-01)
"""

from __future__ import annotations

import json
import socket
from typing import Any

import boto3
from botocore.config import Config

from ..environments import AwsSection, KubernetesSection, NetworkSection
from ..result import Obs, error_text, failed, passed, unknown, worst
from . import kube_auth

_BOTO = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=10)


# --- Kubernetes ---------------------------------------------------------------------------------------------
PARTIAL_METADATA = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"


def _metadata_get(api: Any, path: str, path_params: dict[str, str]) -> int:
    """HTTP status of a metadata-only GET (PartialObjectMetadata): values of Secrets never arrive."""
    method, url, headers, body, post = api.param_serialize(
        method="GET", resource_path=path, path_params=path_params,
        header_params={"Accept": PARTIAL_METADATA}, auth_settings=["BearerToken"])
    resp = api.call_api(method, url, header_params=headers, body=body, post_params=post, _request_timeout=10)
    resp.read()
    return int(resp.status)


class KubernetesProbe:
    def __init__(self, section: KubernetesSection) -> None:
        self.s = section
        self._api: Any = None
        self._error: str | None = None
        self._secret_existence = "not_asked"  # noqa: S105 - a state label, not a secret

    def api_client(self) -> Any:
        if self._api is None and self._error is None:
            try:
                self._api = kube_auth.api_client(self.s)  # kubeconfig, or eks / gke with the gate's own identity
            except Exception as exc:  # noqa: BLE001
                self._error = error_text(exc)
        return self._api

    def _down(self) -> Obs:
        return unknown("cluster_unreachable", error=self._error)

    def _missing(self, api: Any, ns: str, secret: str | None) -> Obs | None:
        """A question about an app that is not deployed is UNKNOWN, never PASS (a SAR on a missing
        namespace is "denied" and would look like a pass). The gate's read identity has no ``get`` on Secrets
        (least privilege: RBAC has no metadata-only verb). Then the Secret's existence is not checked (403) and
        the namespace check alone guards the question; the evidence says so (``secret_existence``)."""
        from kubernetes.client.exceptions import ApiException

        self._secret_existence = "not_asked"  # noqa: S105 - a state label, not a secret
        try:
            if _metadata_get(api, "/api/v1/namespaces/{name}", {"name": ns}) == 404:
                return unknown("app_namespace_missing", namespace=ns)
            if secret:
                try:
                    code = _metadata_get(api, "/api/v1/namespaces/{namespace}/secrets/{name}",
                                         {"namespace": ns, "name": secret})
                except ApiException as exc:
                    code = int(exc.status or 0)
                if code == 404:
                    return unknown("app_secret_missing", secret=f"{ns}/{secret}")
                self._secret_existence = ("not_checked_no_get_right" if code == 403
                                          else "present" if code == 200 else f"http_{code}")
        except Exception as exc:  # noqa: BLE001
            return unknown("cluster_read_error", error=error_text(exc))
        return None

    def secret_read(self) -> Obs:
        ns, secret = self.s.app_namespace, self.s.app_secret
        if not ns or not secret or not self.s.other_service_accounts:
            return unknown("secret_probe_not_configured")
        api = self.api_client()
        if api is None:
            return self._down()
        from kubernetes import client

        missing = self._missing(api, ns, secret)
        if missing is not None:
            return missing
        authz = client.AuthorizationV1Api(api)
        parts: list[Obs] = []
        for ref in sorted(self.s.other_service_accounts):
            sa_ns, _, sa = ref.partition("/")
            user = f"system:serviceaccount:{sa_ns}:{sa}"
            groups = ["system:serviceaccounts", f"system:serviceaccounts:{sa_ns}", "system:authenticated"]
            for verb, name in (("get", secret), ("list", None)):
                body = client.V1SubjectAccessReview(spec=client.V1SubjectAccessReviewSpec(
                    user=user, groups=groups,
                    resource_attributes=client.V1ResourceAttributes(namespace=ns, verb=verb, resource="secrets",
                                                                    name=name)))
                try:
                    st = authz.create_subject_access_review(body).status
                except Exception as exc:  # noqa: BLE001
                    parts.append(unknown("sar_error", identity=ref, verb=verb, error=error_text(exc)))
                    continue
                ev = {"identity": ref, "verb": verb, "secret": f"{ns}/{name or '*'}",
                      "secret_existence": self._secret_existence}
                if st.allowed:
                    parts.append(failed("other_identity_can_read_secret", **ev))
                elif st.evaluation_error:
                    # The authorizer could not evaluate every rule: "not allowed" is not a proof (audit B17).
                    parts.append(unknown("sar_evaluation_error", error=str(st.evaluation_error)[:200], **ev))
                else:
                    parts.append(passed("other_identity_denied", **ev))
        return worst(parts)

    def default_deny(self) -> Obs:
        ns = self.s.app_namespace
        if not ns:
            return unknown("app_namespace_not_configured")
        api = self.api_client()
        if api is None:
            return self._down()
        from kubernetes import client

        missing = self._missing(api, ns, None)
        if missing is not None:
            return missing
        try:
            items = client.NetworkingV1Api(api).list_namespaced_network_policy(ns).items
        except Exception as exc:  # noqa: BLE001
            return unknown("list_error", error=error_text(exc))
        ingress = egress = False
        names = []
        for p in items:
            sp = p.spec
            selects_all = not (sp.pod_selector.match_labels or sp.pod_selector.match_expressions)
            types = set(sp.policy_types or [])
            if selects_all and "Ingress" in types and not sp.ingress:
                ingress = True
                names.append(p.metadata.name)
            if selects_all and "Egress" in types and not sp.egress:
                egress = True
                names.append(p.metadata.name)
        ev = {"namespace": ns, "default_deny_ingress": ingress, "default_deny_egress": egress,
              "policies": sorted(set(names))}
        return passed("default_deny_present", **ev) if ingress and egress else failed("default_deny_missing", **ev)

    def restore(self) -> Obs:
        ref = self.s.restore_cluster
        if not ref:
            return unknown("restore_cluster_not_configured")
        api = self.api_client()
        if api is None:
            return self._down()
        from kubernetes import client
        from kubernetes.client.exceptions import ApiException

        ns, _, name = ref.partition("/")
        try:
            obj = client.CustomObjectsApi(api).get_namespaced_custom_object(
                "postgresql.cnpg.io", "v1", ns, "clusters", name)
        except ApiException as exc:
            if exc.status == 404:
                return unknown("restore_cluster_missing", cluster=ref)
            return unknown("read_error", cluster=ref, error=error_text(exc))
        except Exception as exc:  # noqa: BLE001
            return unknown("read_error", cluster=ref, error=error_text(exc))
        recovery = ((obj.get("spec") or {}).get("bootstrap") or {}).get("recovery")
        status = obj.get("status") or {}
        ev = {"cluster": ref, "bootstrap_recovery": recovery is not None, "phase": status.get("phase"),
              "ready_instances": status.get("readyInstances")}
        if recovery is None:
            return unknown("cluster_not_from_recovery", **ev)
        if (status.get("readyInstances") or 0) >= 1 and "healthy" in str(status.get("phase", "")).lower():
            return passed("restore_healthy", **ev)
        return failed("restore_not_healthy", **ev)


# --- AWS ------------------------------------------------------------------------------------------------------
class AwsProbe:
    def __init__(self, section: AwsSection) -> None:
        self.s = section
        self.session = boto3.session.Session(profile_name=section.profile, region_name=section.region)

    def _c(self, name: str) -> Any:
        return self.session.client(name, region_name=self.s.region, config=_BOTO)

    def secret_read(self) -> Obs:
        if not self.s.secret_ids or not (self.s.other_principal_arns or self.s.other_principal_roles):
            return unknown("secret_probe_not_configured")
        sm, iam = self._c("secretsmanager"), self._c("iam")
        parts: list[Obs] = []
        principals = set(self.s.other_principal_arns)
        for role in sorted(self.s.other_principal_roles):
            try:
                principals.add(iam.get_role(RoleName=role)["Role"]["Arn"])
            except Exception as exc:  # noqa: BLE001
                parts.append(unknown("principal_unresolved", role=role, error=error_text(exc)))
        for sid in sorted(self.s.secret_ids):
            try:
                arn = sm.describe_secret(SecretId=sid)["ARN"]
                rp = sm.get_resource_policy(SecretId=sid).get("ResourcePolicy")
            except Exception as exc:  # noqa: BLE001
                parts.append(unknown("secret_read_error", secret=sid, error=error_text(exc)))
                continue
            for principal in sorted(principals):
                kwargs: dict[str, Any] = {"PolicySourceArn": principal, "ResourceArns": [arn],
                                          "ActionNames": ["secretsmanager:GetSecretValue"]}
                if rp:
                    kwargs["ResourcePolicy"] = rp
                try:
                    res = iam.simulate_principal_policy(**kwargs)["EvaluationResults"][0]
                except Exception as exc:  # noqa: BLE001
                    parts.append(unknown("simulation_error", secret=sid, principal=principal, error=error_text(exc)))
                    continue
                ev = {"secret": sid, "principal": principal, "decision": res["EvalDecision"]}
                parts.append(failed("other_identity_can_read_secret", **ev) if res["EvalDecision"] == "allowed"
                             else passed("other_identity_denied", **ev))
        return worst(parts)

    def secret_cmk(self) -> Obs:
        if not self.s.secret_ids:
            return unknown("secrets_not_configured")
        sm, kms = self._c("secretsmanager"), self._c("kms")
        parts: list[Obs] = []
        for sid in sorted(self.s.secret_ids):
            try:
                key = sm.describe_secret(SecretId=sid).get("KmsKeyId")
            except Exception as exc:  # noqa: BLE001
                parts.append(unknown("secret_read_error", secret=sid, error=error_text(exc)))
                continue
            if not key:
                parts.append(failed("aws_managed_key", secret=sid, key="aws/secretsmanager"))
                continue
            try:
                manager = kms.describe_key(KeyId=key)["KeyMetadata"]["KeyManager"]
            except Exception as exc:  # noqa: BLE001
                parts.append(unknown("kms_read_error", secret=sid, error=error_text(exc)))
                continue
            parts.append(passed("customer_managed_key", secret=sid) if manager == "CUSTOMER"
                         else failed("aws_managed_key", secret=sid, key_manager=manager))
        return worst(parts)

    def eks_secrets_cmk(self) -> Obs:
        """EKS envelope encryption of Kubernetes Secrets (encryptionConfig, resources=secrets) with a CUSTOMER key."""
        if not self.s.eks_clusters:
            return unknown("eks_clusters_not_configured")
        eks, kms = self._c("eks"), self._c("kms")
        parts: list[Obs] = []
        for name in sorted(self.s.eks_clusters):
            try:
                cfg = eks.describe_cluster(name=name)["cluster"].get("encryptionConfig") or []
            except Exception as exc:  # noqa: BLE001
                parts.append(unknown("eks_read_error", cluster=name, error=error_text(exc)))
                continue
            keys = [c.get("provider", {}).get("keyArn") for c in cfg if "secrets" in (c.get("resources") or [])]
            if not keys or not keys[0]:
                parts.append(failed("cluster_secrets_not_encrypted", cluster=name))
                continue
            try:
                manager = kms.describe_key(KeyId=keys[0])["KeyMetadata"]["KeyManager"]
            except Exception as exc:  # noqa: BLE001
                parts.append(unknown("kms_read_error", cluster=name, error=error_text(exc)))
                continue
            parts.append(passed("cluster_secrets_encrypted", cluster=name, key_manager=manager) if manager == "CUSTOMER"
                         else failed("cluster_secrets_aws_managed_key", cluster=name, key_manager=manager))
        return worst(parts)

    def audit(self) -> Obs:
        ct = self._c("cloudtrail")
        try:
            trails = ct.describe_trails(trailNameList=list(self.s.cloudtrail_trails),
                                        includeShadowTrails=False)["trailList"]
        except Exception as exc:  # noqa: BLE001
            return unknown("cloudtrail_read_error", error=error_text(exc))
        logging_trails = []
        for t in sorted(trails, key=lambda x: x["TrailARN"]):
            try:
                if not (ct.get_trail_status(Name=t["TrailARN"]).get("IsLogging") and t.get("IsMultiRegionTrail")):
                    continue
                if _records_management_writes(ct.get_event_selectors(TrailName=t["TrailARN"])):
                    logging_trails.append(t["Name"])
            except Exception as exc:  # noqa: BLE001
                return unknown("cloudtrail_read_error", trail=t["Name"], error=error_text(exc))
        parts = [passed("cloudtrail_logging_management_events", trails=logging_trails) if logging_trails
                 else failed("no_multiregion_trail_with_management_events", trails_seen=len(trails))]
        eks = self._c("eks")
        for name in sorted(self.s.eks_clusters):
            try:
                logs = eks.describe_cluster(name=name)["cluster"].get("logging", {}).get("clusterLogging", [])
            except Exception as exc:  # noqa: BLE001
                parts.append(unknown("eks_read_error", cluster=name, error=error_text(exc)))
                continue
            on = {t for entry in logs if entry.get("enabled") for t in entry.get("types", [])}
            parts.append(passed("eks_audit_log_on", cluster=name) if "audit" in on
                         else failed("eks_audit_log_off", cluster=name, enabled=sorted(on)))
        return worst(parts)


def _records_management_writes(sel: dict[str, Any]) -> bool:
    """Does the trail record management write events (logins, IAM and policy changes)? (audit B17)

    Basic selectors: IncludeManagementEvents with ReadWriteType All or WriteOnly.
    Advanced selectors: a selector with eventCategory = Management and no readOnly = true restriction.
    """
    for s in sel.get("EventSelectors") or []:
        if s.get("IncludeManagementEvents") and s.get("ReadWriteType", "All") in ("All", "WriteOnly"):
            return True
    for s in sel.get("AdvancedEventSelectors") or []:
        fields = {f.get("Field"): f for f in s.get("FieldSelectors") or []}
        cat = fields.get("eventCategory") or {}
        ro = fields.get("readOnly") or {}
        if "Management" in (cat.get("Equals") or []) and "true" not in (ro.get("Equals") or []):
            return True
    return False


# --- Network --------------------------------------------------------------------------------------------------
def _tcp(path: str, timeout_s: float) -> str:
    host, _, port = path.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return "connected"
    except TimeoutError:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except socket.gaierror:
        return "dns_error"
    except OSError as exc:
        return f"error:{type(exc).__name__}"


def network_paths(section: NetworkSection, kube: KubernetesProbe | None = None) -> Obs:
    if not section.control_paths:
        return unknown("control_path_not_configured")
    if section.probe_from == "pod":
        return _paths_from_pod(section, kube)
    control = {p: _tcp(p, section.timeout_s) for p in sorted(section.control_paths)}
    denied = {p: _tcp(p, section.timeout_s) for p in sorted(section.denied_paths)}
    return judge_paths(control, denied, {"probe_from": "gate"})


def _paths_from_pod(section: NetworkSection, kube: KubernetesProbe | None) -> Obs:
    """The connects run in a short-lived Pod in the app namespace with the app's labels and image (netpod.py)."""
    from .netpod import run_in_pod

    if kube is None or not kube.s.app_namespace or not section.app_deployment:
        return unknown("probe_pod_not_configured", detail="needs kubernetes.app_namespace and network.app_deployment")
    api = kube.api_client()
    if api is None:
        return unknown("cluster_unreachable", error=kube._error)
    paths = sorted(section.control_paths) + sorted(section.denied_paths)
    try:
        res, facts = run_in_pod(api, kube.s.app_namespace, section.app_deployment, paths, section.timeout_s)
    except Exception as exc:  # noqa: BLE001
        return unknown("probe_pod_failed", error=error_text(exc))
    control = {p: res.get(p, "missing") for p in sorted(section.control_paths)}
    denied = {p: res.get(p, "missing") for p in sorted(section.denied_paths)}
    facts.pop("pod_phase", None)
    return judge_paths(control, denied, {"probe_from": "pod", **facts})


def judge_paths(control: dict[str, str], denied: dict[str, str], extra: dict[str, Any]) -> Obs:
    ev = {"control_paths": control, "denied_paths": denied, **extra}
    if any(v != "connected" for v in control.values()):
        return unknown("control_path_failed", **ev)
    if not denied:
        return unknown("denied_path_not_configured", **ev)
    if any(v == "connected" for v in denied.values()):
        return failed("denied_path_reachable", **ev)
    if any(v not in ("refused", "timeout") for v in denied.values()):
        # DNS failures and other errors say nothing about the network policy (audit B17).
        return unknown("denied_path_inconclusive", **ev)
    return passed("only_allowed_paths_reachable", **ev)


def dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
