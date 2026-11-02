"""Provider-neutral control questions (lead decision 2026-11-03, spec "6개 통제를 양쪽에서 같은 방법으로 시험").

Each question asks the same thing of every environment; only the probe behind it changes with ``platform.kind``.

SEC-SECRET-STORE-READ  another identity cannot read the app's secrets in their store of record
    eks   iam:SimulatePrincipalPolicy of other principals on secretsmanager:GetSecretValue (resource policy included)
    rke2, gke  the platform OpenBao: the gate's ``other`` Kubernetes-auth role (no grant) must get 403 on the KV
          data of every app secret path, while its ``meta`` role sees that the path exists
SEC-SECRET-AT-REST     app secrets at rest are under a key the customer side controls, in the store and in the cluster
    eks   Secrets Manager KmsKeyId is a customer-managed KMS key + EKS encryptionConfig (secrets) with a CUSTOMER key
    rke2  OpenBao is the store (unsealed, Shamir, path present) + kube-apiserver encrypts Secrets (/metrics transformer
          prefix k8s:enc:<provider>, not identity; static Pod flag --encryption-provider-config)
    gke   OpenBao is the store + GKE application-layer secrets encryption (databaseEncryption ENCRYPTED, Cloud KMS key)
AUD-CONTROL-PLANE      control-plane audit is on and recording now
    eks   multi-region CloudTrail with management write events + EKS ``audit`` log type
    rke2  kube-apiserver audit flags (policy file, log path) + apiserver_audit_event_total grows during the probe
    gke   Cloud Audit Logs admin-activity entries for the cluster in the last hour (logging entries.list)
Evidence carries no counters or timestamps that change between runs (exception digests stay stable).
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..environments import Environment, SecretStoreSection
from ..result import Obs, error_text, failed, passed, unknown, worst

ENCRYPTING_PROVIDERS = frozenset({"aescbc", "aesgcm", "secretbox", "kms"})
_METRIC = re.compile(r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def parse_metrics(text: str, names: set[str]) -> list[tuple[str, dict[str, str], float]]:
    """Prometheus text exposition -> (name, labels, value) for the given metric names."""
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _METRIC.match(line)
        if not m or m["name"] not in names:
            continue
        try:
            value = float(m["value"])
        except ValueError:
            continue
        out.append((m["name"], dict(_LABEL.findall(m["labels"] or "")), value))
    return out


def judge_secret_transformers(series: list[tuple[str, dict[str, str], float]]) -> Obs:
    """kube-apiserver Secret storage transformers seen in /metrics (resource=secrets, status=OK)."""
    providers: set[str] = set()
    for _, labels, value in series:
        if labels.get("resource", "secrets") != "secrets" or labels.get("status") != "OK" or value <= 0:
            continue
        prefix = labels.get("transformer_prefix", "")
        m = re.match(r"^k8s:enc:([a-z0-9]+):", prefix)
        if m:
            providers.add(m.group(1))
        elif prefix in ("", "identity"):
            providers.add("identity")
        # other prefixes are key names inside a provider (RKE2: "aescbckey:"), not providers
    if not providers:
        return unknown("no_secret_storage_transformations")
    ev = {"secret_transformers": sorted(providers)}
    if providers & ENCRYPTING_PROVIDERS:
        key = ("kms plugin" if "kms" in providers
               else "local key in the server's encryption config (RKE2 secrets-encrypt)")
        return passed("cluster_secrets_encrypted", key_custody=key, **ev)
    return failed("cluster_secrets_not_encrypted", **ev)


# --- OpenBao (platform) ------------------------------------------------------------------------------------------
class OpenBaoProbe:
    def __init__(self, s: SecretStoreSection) -> None:
        self.s = s

    def _req(self, method: str, path: str, token: str | None = None, json: Any = None) -> Any:
        import requests

        headers = {"X-Vault-Token": token} if token else {}
        return requests.request(method, f"{self.s.address.rstrip('/')}/v1/{path}", headers=headers, json=json,
                                timeout=self.s.timeout_s, verify=self.s.ca_file or True)

    def _login(self, role: str) -> str:
        jwt = Path(self.s.jwt_file).read_text(encoding="utf-8").strip()
        r = self._req("POST", f"auth/{self.s.auth_mount}/login", json={"role": role, "jwt": jwt})
        if r.status_code != 200:
            raise RuntimeError(f"login role {role}: HTTP {r.status_code}")
        return r.json()["auth"]["client_token"]

    def _revoke(self, token: str) -> None:
        try:
            self._req("POST", "auth/token/revoke-self", token)
        except Exception:  # noqa: BLE001, S110 - best effort; the token has a 5 min TTL
            pass

    def _present(self) -> tuple[dict[str, str], Obs | None]:
        """Metadata of every app secret path with the meta role: present | missing | http_<code>."""
        if not self.s.paths:
            return {}, unknown("secret_paths_not_configured")
        try:
            tok = self._login(self.s.meta_role)
        except Exception as exc:  # noqa: BLE001
            return {}, unknown("openbao_login_failed", role=self.s.meta_role, error=error_text(exc))
        try:
            seen = {}
            for p in sorted(self.s.paths):
                code = self._req("GET", f"{self.s.kv_mount}/metadata/{p}", tok).status_code
                seen[p] = "present" if code == 200 else "missing" if code == 404 else f"http_{code}"
        finally:
            self._revoke(tok)
        return seen, None

    def other_identity_denied(self) -> Obs:
        seen, err = self._present()
        if err is not None:
            return err
        ev: dict[str, Any] = {"store": "platform OpenBao", "paths": seen, "other_role": self.s.other_role}
        if any(v != "present" for v in seen.values()):
            return unknown("secret_path_not_present", **ev)
        try:
            tok = self._login(self.s.other_role)
        except Exception as exc:  # noqa: BLE001
            return unknown("openbao_login_failed", role=self.s.other_role, error=error_text(exc), **ev)
        try:
            reads = {p: self._req("GET", f"{self.s.kv_mount}/data/{p}", tok).status_code for p in sorted(self.s.paths)}
        finally:
            self._revoke(tok)
        ev["other_read"] = {p: f"http_{c}" for p, c in reads.items()}
        if any(c == 200 for c in reads.values()):
            return failed("other_identity_can_read_secret", **ev)
        if all(c == 403 for c in reads.values()):
            return passed("other_identity_denied", **ev)
        return unknown("other_read_inconclusive", **ev)

    def store_facts(self) -> Obs:
        try:
            st = self._req("GET", "sys/seal-status").json()
        except Exception as exc:  # noqa: BLE001
            return unknown("openbao_unreachable", error=error_text(exc))
        seen, err = self._present()
        ev: dict[str, Any] = {"store": "platform OpenBao", "seal_type": st.get("type"), "sealed": st.get("sealed"),
                              "initialized": st.get("initialized"), "paths": seen,
                              "at_rest": "barrier AES-256-GCM, root key under Shamir unseal keys held by the platform"}
        if err is not None:
            return Obs(err.verdict, err.reason, {**err.evidence, **ev})
        if st.get("sealed") is not False or not st.get("initialized"):
            return unknown("openbao_sealed", **ev)
        if any(v == "missing" for v in seen.values()):
            return failed("secret_not_in_store", **ev)
        if any(v != "present" for v in seen.values()):
            return unknown("secret_path_unreadable", **ev)
        return passed("secrets_in_openbao", **ev)


# --- Google APIs -------------------------------------------------------------------------------------------------
def _google_session() -> Any:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=GOOGLE_SCOPES)
    return AuthorizedSession(creds)


def gke_cluster_fields(cluster: str, fields: str) -> dict[str, Any]:
    r = _google_session().get(f"https://container.googleapis.com/v1/{cluster}", params={"fields": fields}, timeout=15)
    r.raise_for_status()
    return r.json()


def gke_admin_activity(cluster: str, window: timedelta = timedelta(hours=1)) -> list[dict[str, Any]]:
    parts = cluster.split("/")  # projects/<p>/locations/<l>/clusters/<c>
    project, location, name = parts[1], parts[3], parts[5]
    since = (datetime.now(UTC) - window).strftime("%Y-%m-%dT%H:%M:%SZ")
    flt = (f'logName="projects/{project}/logs/cloudaudit.googleapis.com%2Factivity" AND resource.type="k8s_cluster" '
           f'AND resource.labels.cluster_name="{name}" AND resource.labels.location="{location}" '
           f'AND timestamp>="{since}"')
    r = _google_session().post("https://logging.googleapis.com/v2/entries:list", timeout=20,
                               json={"resourceNames": [f"projects/{project}"], "filter": flt,
                                     "orderBy": "timestamp desc", "pageSize": 5})
    r.raise_for_status()
    return r.json().get("entries", [])


# --- the three questions -----------------------------------------------------------------------------------------
class PlatformProbe:
    def __init__(self, env: Environment, kube: Any = None, aws: Any = None) -> None:
        assert env.platform is not None
        self.env, self.p, self.kube, self.aws = env, env.platform, kube, aws

    def _store(self) -> OpenBaoProbe | None:
        return OpenBaoProbe(self.p.secret_store) if self.p.secret_store else None

    def _need_aws(self) -> Obs | None:
        return None if self.aws is not None else unknown("aws_section_missing", platform=self.p.kind)

    def _need_kube(self) -> Any:
        if self.kube is None:
            return None, unknown("kubernetes_section_missing", platform=self.p.kind)
        api = self.kube.api_client()
        if api is None:
            return None, unknown("cluster_unreachable", error=self.kube._error)
        return api, None

    # SEC-SECRET-STORE-READ
    def secret_store_read(self) -> Obs:
        if self.p.kind == "eks":
            return self._need_aws() or self.aws.secret_read()
        store = self._store()
        return store.other_identity_denied() if store else unknown("secret_store_not_configured")

    # SEC-SECRET-AT-REST
    def secret_at_rest(self) -> Obs:
        if self.p.kind == "eks":
            miss = self._need_aws()
            return miss or worst([self.aws.secret_cmk(), self.aws.eks_secrets_cmk()])
        store = self._store()
        parts = [store.store_facts() if store else unknown("secret_store_not_configured")]
        parts.append(self._rke2_encryption() if self.p.kind == "rke2" else self._gke_encryption())
        return worst(parts)

    # AUD-CONTROL-PLANE
    def control_plane_audit(self) -> Obs:
        if self.p.kind == "eks":
            return self._need_aws() or self.aws.audit()
        if self.p.kind == "rke2":
            return self._rke2_audit()
        return self._gke_audit()

    # --- rke2 ---
    def _apiserver_flags(self, api: Any) -> dict[str, Any] | str:
        ref = self.p.apiserver_pod
        if not ref:
            return "apiserver_pod_not_configured"
        from kubernetes import client

        ns, _, name = ref.partition("/")
        try:
            pod = client.CoreV1Api(api).read_namespaced_pod(name, ns)
        except Exception as exc:  # noqa: BLE001
            return f"unreadable: {error_text(exc)[:120]}"
        flags: dict[str, Any] = {}
        for c in pod.spec.containers:
            for a in (c.command or []) + (c.args or []):
                k, _, v = a.partition("=")
                if k.startswith("--audit-") or k.startswith("--encryption-provider-config"):
                    flags[k] = v
        return flags

    def _metrics(self, api: Any) -> str:
        method, url, headers, body, post = api.param_serialize(method="GET", resource_path="/metrics",
                                                               auth_settings=["BearerToken"])
        resp = api.call_api(method, url, header_params=headers, body=body, post_params=post, _request_timeout=15)
        data = resp.read()
        if int(resp.status) != 200:
            raise RuntimeError(f"/metrics HTTP {resp.status}")
        return data.decode() if isinstance(data, bytes) else str(data)

    def _rke2_encryption(self) -> Obs:
        api, err = self._need_kube()
        if err:
            return err
        try:
            series = parse_metrics(self._metrics(api), {"apiserver_storage_transformation_operations_total"})
        except Exception as exc:  # noqa: BLE001
            return unknown("apiserver_metrics_unreadable", error=error_text(exc))
        obs = judge_secret_transformers(series)
        flags = self._apiserver_flags(api)
        ev = {**obs.evidence, "apiserver_flags": {k: v for k, v in flags.items() if k.startswith("--encryption")}
              if isinstance(flags, dict) else flags}
        return type(obs)(obs.verdict, obs.reason, ev)

    def _rke2_audit(self) -> Obs:
        api, err = self._need_kube()
        if err:
            return err
        names = {"apiserver_audit_event_total", "apiserver_audit_error_total"}
        try:
            first = parse_metrics(self._metrics(api), names)
            time.sleep(2)
            ns = self.env.kubernetes.app_namespace if self.env.kubernetes else None
            if ns:  # one ordinary audited request between the two reads
                from kubernetes import client

                client.CoreV1Api(api).read_namespace(ns)
            second = parse_metrics(self._metrics(api), names)
        except Exception as exc:  # noqa: BLE001
            return unknown("apiserver_metrics_unreadable", error=error_text(exc))

        def total(rows: list[tuple[str, dict[str, str], float]], name: str) -> float | None:
            vals = [v for n, _, v in rows if n == name]
            return sum(vals) if vals else None

        ev_before, ev_after = total(first, "apiserver_audit_event_total"), total(second, "apiserver_audit_event_total")
        er_before, er_after = total(first, "apiserver_audit_error_total"), total(second, "apiserver_audit_error_total")
        flags = self._apiserver_flags(api)
        ev: dict[str, Any] = {
            "apiserver_flags": {k: v for k, v in flags.items() if k.startswith("--audit")} if isinstance(flags, dict)
            else flags,
            "audit_events_grew_during_probe": None if ev_before is None or ev_after is None else ev_after > ev_before,
            "audit_errors_grew_during_probe": None if er_before is None or er_after is None else er_after > er_before,
        }
        if isinstance(flags, dict) and not any(k in flags for k in ("--audit-log-path", "--audit-webhook-config-file")):
            return failed("audit_backend_off", **ev)
        if ev["audit_events_grew_during_probe"] is None:
            return unknown("audit_metrics_missing", **ev)
        if not ev["audit_events_grew_during_probe"]:
            return failed("no_recent_audit_events", **ev)
        if ev["audit_errors_grew_during_probe"]:
            return failed("audit_backend_errors", **ev)
        return passed("apiserver_audit_recording", **ev)

    # --- gke ---
    def _gke_encryption(self) -> Obs:
        if not self.p.gke_cluster:
            return unknown("gke_cluster_not_configured")
        try:
            enc = gke_cluster_fields(self.p.gke_cluster, "databaseEncryption").get("databaseEncryption") or {}
        except Exception as exc:  # noqa: BLE001
            return unknown("gke_cluster_unreadable", error=error_text(exc))
        ev = {"database_encryption": {"state": enc.get("state"), "current_state": enc.get("currentState"),
                                      "key": enc.get("keyName")}}
        if enc.get("state") == "ENCRYPTED" and enc.get("keyName") and \
                enc.get("currentState", "CURRENT_STATE_ENCRYPTED") == "CURRENT_STATE_ENCRYPTED":
            return passed("cluster_secrets_encrypted",
                          key_custody="Cloud KMS key (application-layer secrets encryption)", **ev)
        return failed("cluster_secrets_not_encrypted", **ev)

    def _gke_audit(self) -> Obs:
        if not self.p.gke_cluster:
            return unknown("gke_cluster_not_configured")
        try:
            entries = gke_admin_activity(self.p.gke_cluster)
        except Exception as exc:  # noqa: BLE001
            return unknown("audit_logs_unreadable", error=error_text(exc))
        ev = {"log": "cloudaudit.googleapis.com/activity", "resource": "k8s_cluster", "window": "1h",
              "entries_found": bool(entries)}
        return passed("admin_activity_recorded", **ev) if entries else failed("no_admin_activity_last_hour", **ev)
