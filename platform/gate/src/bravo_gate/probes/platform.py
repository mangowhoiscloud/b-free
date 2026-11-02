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
import os
import socket
from typing import Any

import boto3
from botocore.config import Config

from ..environments import AwsSection, KubernetesSection, NetworkSection
from ..result import Obs, error_text, failed, passed, unknown, worst

_BOTO = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=10)


# --- Kubernetes ---------------------------------------------------------------------------------------------
class KubernetesProbe:
    def __init__(self, section: KubernetesSection) -> None:
        self.s = section
        self._api: Any = None
        self._error: str | None = None

    def api_client(self) -> Any:
        if self._api is None and self._error is None:
            try:
                from kubernetes import client, config

                path = os.environ.get(self.s.kubeconfig_env) if self.s.kubeconfig_env else None
                if self.s.kubeconfig_env and not path:
                    raise RuntimeError(f"kubeconfig_env {self.s.kubeconfig_env} is not set")
                cfg = client.Configuration()
                config.load_kube_config(config_file=path, context=self.s.context, client_configuration=cfg)
                self._api = client.ApiClient(cfg)
            except Exception as exc:  # noqa: BLE001
                self._error = error_text(exc)
        return self._api

    def _down(self) -> Obs:
        return unknown("cluster_unreachable", error=self._error)

    def secret_read(self) -> Obs:
        ns, secret = self.s.app_namespace, self.s.app_secret
        if not ns or not secret or not self.s.other_service_accounts:
            return unknown("secret_probe_not_configured")
        api = self.api_client()
        if api is None:
            return self._down()
        from kubernetes import client

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
                ev = {"identity": ref, "verb": verb, "secret": f"{ns}/{name or '*'}"}
                parts.append(failed("other_identity_can_read_secret", **ev) if st.allowed
                             else passed("other_identity_denied", **ev))
        return worst(parts)

    def default_deny(self) -> Obs:
        ns = self.s.app_namespace
        if not ns:
            return unknown("app_namespace_not_configured")
        api = self.api_client()
        if api is None:
            return self._down()
        from kubernetes import client

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
        if not self.s.secret_ids or not self.s.other_principal_arns:
            return unknown("secret_probe_not_configured")
        sm, iam = self._c("secretsmanager"), self._c("iam")
        parts: list[Obs] = []
        for sid in sorted(self.s.secret_ids):
            try:
                arn = sm.describe_secret(SecretId=sid)["ARN"]
                rp = sm.get_resource_policy(SecretId=sid).get("ResourcePolicy")
            except Exception as exc:  # noqa: BLE001
                parts.append(unknown("secret_read_error", secret=sid, error=error_text(exc)))
                continue
            for principal in sorted(self.s.other_principal_arns):
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
                if ct.get_trail_status(Name=t["TrailARN"]).get("IsLogging") and t.get("IsMultiRegionTrail"):
                    logging_trails.append(t["Name"])
            except Exception as exc:  # noqa: BLE001
                return unknown("cloudtrail_read_error", trail=t["Name"], error=error_text(exc))
        parts = [passed("cloudtrail_logging", trails=logging_trails) if logging_trails
                 else failed("no_logging_multiregion_trail", trails_seen=len(trails))]
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
    except OSError as exc:
        return f"error:{type(exc).__name__}"


def network_paths(section: NetworkSection) -> Obs:
    if not section.control_paths:
        return unknown("control_path_not_configured")
    control = {p: _tcp(p, section.timeout_s) for p in sorted(section.control_paths)}
    denied = {p: _tcp(p, section.timeout_s) for p in sorted(section.denied_paths)}
    ev = {"control_paths": control, "denied_paths": denied}
    if any(v != "connected" for v in control.values()):
        return unknown("control_path_failed", **ev)
    if not denied:
        return unknown("denied_path_not_configured", **ev)
    if any(v == "connected" for v in denied.values()):
        return failed("denied_path_reachable", **ev)
    return passed("only_allowed_paths_reachable", **ev)


def dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
