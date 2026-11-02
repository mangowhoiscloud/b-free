"""NET-PATHS from inside the app namespace (lead decision 2026-11-03).

The gate starts one short-lived Pod in ``kubernetes.app_namespace`` with the app Deployment's Pod labels and image
(so the app's NetworkPolicies select it), runs plain TCP connects to the configured control and denied paths, reads
the result from the Pod log and deletes the Pod. The Pod: no service account token, no Service links, restricted
security context, never Ready (an always-failing readiness probe keeps it out of the app's Services), at most 90 s.
It does not carry ``pod-template-hash``, so no ReplicaSet adopts it.
"""

from __future__ import annotations

import json
import time
from typing import Any

PROBE_LABEL = "bravo.io/probe"
RESULT_PREFIX = "BRAVO_NET_PATHS "
SCRIPT = r"""
import json, socket, sys
paths, t = json.loads(sys.argv[1]), float(sys.argv[2])
out = {}
for p in paths:
    h, _, port = p.rpartition(":")
    try:
        with socket.create_connection((h, int(port)), timeout=t):
            out[p] = "connected"
    except TimeoutError:
        out[p] = "timeout"
    except ConnectionRefusedError:
        out[p] = "refused"
    except socket.gaierror:
        out[p] = "dns_error"
    except OSError as e:
        out[p] = "error:" + type(e).__name__
print("BRAVO_NET_PATHS " + json.dumps(out, sort_keys=True), flush=True)
"""


def probe_pod_spec(deployment: Any, paths: list[str], timeout_s: float) -> Any:
    from kubernetes import client

    tpl = deployment.spec.template
    labels = {k: v for k, v in (tpl.metadata.labels or {}).items() if k != "pod-template-hash"}
    labels[PROBE_LABEL] = "gate-net-paths"
    image = tpl.spec.containers[0].image
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            generate_name="gate-netprobe-", labels=labels,
            annotations={"bravo.io/purpose": "platform gate NET-PATHS probe; deleted after the run"}),
        spec=client.V1PodSpec(
            restart_policy="Never", active_deadline_seconds=90, termination_grace_period_seconds=0,
            automount_service_account_token=False, enable_service_links=False,
            security_context=tpl.spec.security_context or client.V1PodSecurityContext(
                run_as_non_root=True, run_as_user=10001,
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault")),
            containers=[client.V1Container(
                name="probe", image=image, command=["python", "-c", SCRIPT, json.dumps(paths), str(timeout_s)],
                security_context=client.V1SecurityContext(
                    allow_privilege_escalation=False, read_only_root_filesystem=True, run_as_non_root=True,
                    capabilities=client.V1Capabilities(drop=["ALL"]),
                    seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault")),
                resources=client.V1ResourceRequirements(requests={"cpu": "10m", "memory": "32Mi"},
                                                        limits={"memory": "96Mi"}),
                readiness_probe=client.V1Probe(_exec=client.V1ExecAction(command=["/bin/false"]), period_seconds=30),
            )],
        ),
    )


def run_in_pod(api: Any, namespace: str, deployment_name: str, paths: list[str], timeout_s: float,
               wait_s: float = 75.0) -> tuple[dict[str, str], dict[str, Any]]:
    """Connect results per path and pod facts for the evidence. Raises on setup errors (caller -> UNKNOWN)."""
    from kubernetes import client

    core, apps = client.CoreV1Api(api), client.AppsV1Api(api)
    dep = apps.read_namespaced_deployment(deployment_name, namespace)
    pod = core.create_namespaced_pod(namespace, probe_pod_spec(dep, paths, timeout_s))
    name = pod.metadata.name
    facts: dict[str, Any] = {"namespace": namespace, "labels_from": f"deployment/{deployment_name}",
                             "image": dep.spec.template.spec.containers[0].image}
    try:
        deadline = time.monotonic() + wait_s
        phase = None
        while time.monotonic() < deadline:
            phase = core.read_namespaced_pod(name, namespace).status.phase
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(1)
        facts["pod_phase"] = phase
        log = core.read_namespaced_pod_log(name, namespace, container="probe")
        for line in reversed(log.splitlines()):
            if line.startswith(RESULT_PREFIX):
                return json.loads(line[len(RESULT_PREFIX):]), facts
        raise RuntimeError(f"probe pod {phase}: no result line")
    finally:
        try:
            core.delete_namespaced_pod(name, namespace, grace_period_seconds=0)
            facts["cleanup"] = "deleted"
        except Exception as exc:  # noqa: BLE001
            facts["cleanup"] = f"delete failed: {type(exc).__name__}"
