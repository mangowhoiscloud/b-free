"""Destinations. ``k3s-containerd``: on-prem K3s without a registry (CLOUD-E2E-CONTRACT §5). The OCI archive is
piped into every node's containerd (``k3s ctr -n k8s.io images import``) and named ``<repo>@<digest>`` the same
way deploy/landing-zone/install.sh does. Deploy happens only when every node holds exactly the approved digest,
and is accepted only when every new pod runs it; otherwise the rollout is undone.

``gke-artifact-registry``: reported NOT_CONNECTED unless a kubectl context for the GKE cluster exists, and even
then this driver does not push (no platform credential for Artifact Registry is wired yet).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runtime import CommandError, Result, Step, Stop, clean_env, run

KUBE_ENV = ("KUBECONFIG",)


def contexts(kubectl: str) -> list[dict[str, str]]:
    out = run([kubectl, "config", "view", "-o", "json"], env=clean_env(keep=KUBE_ENV), timeout=30).stdout
    cfg = json.loads(out)
    clusters = {c["name"]: c.get("cluster", {}).get("server", "") for c in cfg.get("clusters") or []}
    return [{"name": c["name"], "cluster": c.get("context", {}).get("cluster", ""),
             "server": clusters.get(c.get("context", {}).get("cluster", ""), "")} for c in cfg.get("contexts") or []]


def resolve_context(kubectl: str, dest: dict[str, Any]) -> str:
    known = contexts(kubectl)
    names = {c["name"] for c in known}
    if dest.get("kube_context"):
        if dest["kube_context"] not in names:
            raise Stop("failed", "kube_context_missing", Result.NOT_CONNECTED,
                       {"context": dest["kube_context"], "available": sorted(names)})
        return dest["kube_context"]
    needle = dest.get("detect_context_cluster_contains")
    hits = [c["name"] for c in known if needle and (needle in c["cluster"] or needle in c["name"])]
    if len(hits) != 1:
        raise Stop("failed", "kube_context_missing", Result.NOT_CONNECTED,
                   {"detect": needle, "matches": hits, "available": sorted(names)})
    return hits[0]


class K3sContainerd:
    def __init__(self, dest: dict[str, Any], kubectl: str, limactl: str) -> None:
        self.d = dest
        self.kubectl = kubectl
        self.limactl = limactl
        self.ctx = resolve_context(kubectl, dest)
        # containerd CLI on the node: K3s ``k3s ctr``; RKE2 ``/var/lib/rancher/rke2/bin/ctr --address
        # /run/k3s/containerd/containerd.sock`` (the on-prem cluster was rebuilt as RKE2 on 2026-11-02).
        self.ctr = dest.get("ctr", "k3s ctr")

    def k(self, *args: str, timeout: float = 60, check: bool = True, input_bytes: bytes | None = None):
        return run([self.kubectl, "--context", self.ctx, *args], env=clean_env(keep=KUBE_ENV), timeout=timeout,
                   check=check, input_bytes=input_bytes)

    def reachable(self) -> dict[str, Any]:
        try:
            self.k("get", "--raw", "/readyz", timeout=20)
        except (CommandError, OSError) as exc:
            raise Stop("failed", "onprem_unreachable", Result.NOT_CONNECTED, {"context": self.ctx, "error": str(exc)[-300:]}) from exc
        nodes = json.loads(self.k("get", "nodes", "-o", "json").stdout)["items"]
        k8s_nodes = sorted(n["metadata"]["name"] for n in nodes)
        raw = run([self.limactl, "list", "--json"], env=clean_env(), timeout=30).stdout.decode()
        lima = [json.loads(line) for line in raw.splitlines() if line.strip()]  # one JSON object per line
        running = sorted(i["name"] for i in lima if i.get("status") == "Running")
        missing = [n for n in self.d["lima_nodes"] if n not in running]
        if missing or sorted(self.d["lima_nodes"]) != k8s_nodes:
            raise Stop("failed", "onprem_nodes_unreachable", Result.NOT_CONNECTED,
                       {"k8s_nodes": k8s_nodes, "lima_running": running, "configured": self.d["lima_nodes"]})
        return {"context": self.ctx, "nodes": k8s_nodes}

    def _sh(self, node: str, cmd: str, input_bytes: bytes | None = None, timeout: float = 600, stdin: Any = None):
        return run([self.limactl, "shell", node, "--", "sh", "-c", cmd], env=clean_env(), timeout=timeout,
                   input_bytes=input_bytes, stdin=stdin)

    def node_digest(self, node: str, ref: str) -> str | None:
        """DIGEST column of ``ref`` in the node's containerd (k8s.io namespace), or None."""
        out = self._sh(node, f"sudo {self.ctr} -n k8s.io images ls 'name=={ref}'", timeout=60).stdout.decode()
        for line in out.splitlines()[1:]:
            cols = line.split()
            if cols and cols[0] == ref and len(cols) >= 3:
                return cols[2]
        return None

    def import_image(self, tar: Path, digest: str, archive_name: str) -> Step:
        repo = self.d["image_repository"]
        ref = f"{repo}@{digest}"
        per_node = {}
        for node in self.d["lima_nodes"]:
            have = self.node_digest(node, ref)
            if have == digest:
                per_node[node] = {"action": "present", "digest": have}
                continue
            tmp = f"/tmp/bravo-apply-{digest[7:19]}.tar"  # noqa: S108 - path on the K3s node, root-owned import
            with tar.open("rb") as f:
                self._sh(node, f"cat > {tmp}", stdin=f, timeout=900)
            self._sh(node, f"sudo {self.ctr} -n k8s.io images import --platform linux/arm64 {tmp} >/dev/null", timeout=900)
            # The archive's own name is kept: containerd's CRI image store remembers the first name it saw for an
            # image, and removing it makes the container start fail ("failed to get image ... not found", measured
            # 2026-11-03 on bravo-onprem with the gate image). Only the digest name is used by the Deployment.
            self._sh(node, f"sudo {self.ctr} -n k8s.io images tag --force '{archive_name}' '{ref}' >/dev/null"
                           f"; rm -f {tmp}", timeout=120)
            have = self.node_digest(node, ref)
            per_node[node] = {"action": "imported", "digest": have}
            if have != digest:
                raise Stop("refused", "destination_digest_mismatch", Result.REFUSED,
                           {"node": node, "expected": digest, "found": have, "ref": ref})
        return Step(detail={"ref": ref, "nodes": per_node, "method": "OCI archive piped to k3s ctr import (no registry)"})

    def precheck(self, digest: str) -> Step:
        ref = f"{self.d['image_repository']}@{digest}"
        found = {node: self.node_digest(node, ref) for node in self.d["lima_nodes"]}
        bad = {n: d for n, d in found.items() if d != digest}
        if bad:
            raise Stop("refused", "destination_digest_mismatch", Result.REFUSED, {"expected": digest, "found": found})
        return Step(detail={"ref": ref, "nodes": found})

    def deployment(self) -> dict[str, Any]:
        return json.loads(self.k("-n", self.d["namespace"], "get", "deploy", self.d["deployment"], "-o", "json").stdout)

    def current_image(self) -> str:
        dep = self.deployment()
        for c in dep["spec"]["template"]["spec"]["containers"]:
            if c["name"] == self.d["container"]:
                return c["image"]
        raise Stop("failed", "container_not_found", Result.FAIL, {"container": self.d["container"]})

    def rollout(self, digest: str, change_cause: str) -> Step:
        ns, name, ctr = self.d["namespace"], self.d["deployment"], self.d["container"]
        ref = f"{self.d['image_repository']}@{digest}"
        before = self.current_image()
        if before == ref:
            return Step(detail={"before": before, "after": ref, "action": "already_running"})
        self.k("-n", ns, "annotate", "deploy", name, f"kubernetes.io/change-cause={change_cause}", "--overwrite")
        self.k("-n", ns, "set", "image", f"deploy/{name}", f"{ctr}={ref}")
        try:
            self.k("-n", ns, "rollout", "status", f"deploy/{name}", f"--timeout={self.d.get('rollout_timeout_s', 240)}s",
                   timeout=self.d.get("rollout_timeout_s", 240) + 30)
        except CommandError as exc:
            self.undo()
            raise Stop("failed", "rollout_failed", Result.FAIL, {"before": before, "attempted": ref, "error": exc.stderr,
                                                                  "rolled_back": True}) from exc
        return Step(detail={"before": before, "after": ref, "action": "rolled_out"})

    def undo(self) -> None:
        ns, name = self.d["namespace"], self.d["deployment"]
        self.k("-n", ns, "rollout", "undo", f"deploy/{name}", check=False)
        self.k("-n", ns, "rollout", "status", f"deploy/{name}", "--timeout=180s", timeout=210, check=False)

    def running_digests(self) -> dict[str, Any]:
        dep = self.deployment()
        ns = self.d["namespace"]
        sel = ",".join(f"{k}={v}" for k, v in dep["spec"]["selector"]["matchLabels"].items())
        pods = json.loads(self.k("-n", ns, "get", "pods", "-l", sel, "-o", "json").stdout)["items"]
        out = []
        for p in pods:
            if p["metadata"].get("deletionTimestamp"):
                continue
            st = next((c for c in p.get("status", {}).get("containerStatuses") or [] if c["name"] == self.d["container"]), {})
            out.append({"pod": p["metadata"]["name"], "node": p["spec"].get("nodeName"), "ready": st.get("ready"),
                        "image": st.get("image"), "imageID": st.get("imageID")})
        return {"replicas": dep["spec"].get("replicas"), "pods": out}

    def verify(self, digest: str) -> Step:
        state = self.running_digests()
        pods = state["pods"]
        ok = (len(pods) == state["replicas"] and pods
              and all(p["ready"] and str(p["imageID"]).endswith("@" + digest) for p in pods))
        if not ok:
            self.undo()
            raise Stop("refused", "deployed_digest_mismatch", Result.REFUSED, {**state, "expected": digest, "rolled_back": True})
        return Step(detail={**state, "deployed_digest": digest})


def driver(dest: dict[str, Any], kubectl: str, limactl: str) -> K3sContainerd:
    kind = dest.get("kind")
    if kind == "k3s-containerd":
        return K3sContainerd(dest, kubectl, limactl)
    if kind == "gke-artifact-registry":
        ctx = resolve_context(kubectl, dest)  # NOT_CONNECTED when there is no GKE context
        raise Stop("failed", "gke_driver_not_wired", Result.NOT_CONNECTED,
                   {"context": ctx, "why": "no platform credential for Artifact Registry push is wired (D-26)"})
    raise Stop("failed", "destination_kind_unknown", Result.FAIL, {"kind": kind})
