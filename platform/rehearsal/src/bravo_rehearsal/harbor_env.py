"""Harbor 0.25.0 environment: one Kubernetes namespace per Harbor environment (ADR 0001, 05 4).

Harbor creates two environments per Trial from the same ``EnvironmentConfig``:

- the agent environment (``session_id`` ``<trial>__env``) becomes ``rehearsal-<plan>-<n>`` with
  the sandbox Pod ``rehearsal-sandbox-<trial>``;
- the separate verifier environment (``session_id`` ``<trial>__verifier__<key>``, task
  ``[verifier] environment_mode = "separate"``) becomes ``verify-<plan>-<n>`` with the verifier
  Pod ``verify-runner-<trial>`` built from the digest-pinned verifier image.

Two identities, never mixed (05 3 검증 경계, 05 4 연결 조건):

- provisioner (the service kubeconfig): creates and deletes the namespace, its baseline
  (Pod Security ``restricted``, NetworkPolicy default deny + same namespace + kube-dns,
  ResourceQuota, LimitRange, token-less ServiceAccounts), the exec RBAC and the one Pod from a
  fixed template. It never runs a command inside a Pod.
- exec: a namespace-local ServiceAccount used through a short-lived TokenRequest token. Its
  Role allows ``pods/exec`` on the one Pod name only. ``rehearsal-exec`` exists only in the
  rehearsal namespace and ``verify-exec`` only in the verify namespace, so the tool layer's
  identity cannot reach the verifier (V1). Every ``exec``/upload/download re-reads the Pod UID
  first (V3b).

File transfer is a tar archive streamed over the exec channel: base64 lines on stdin ending
with ``EOF`` (upload) and on stdout (download), unpacked with tarfile's ``data`` filter on
both sides. It needs only ``python3`` in the image, not ``tar`` and not a writable root.

Images (both digest-pinned): ``sh``, ``sleep`` and ``python3`` 3.12+ (tarfile ``data`` filter).
The root filesystem is read-only; the role roots ``/src``, ``/work``, ``/out``, ``/app`` (13 4) and
Harbor's ``/tmp``, ``/logs``, ``/tests``, ``/input`` are emptyDirs.

Network: ``no-network`` is enforced by the NetworkPolicy baseline and checked on every start
by egress canaries (internet, cloud metadata) and a privileged-Pod admission probe, in both
namespaces, before anything enters the Pod. Any open path stops the start
(``isolation_check_failed``), so the ``disable_internet`` capability holds only for
environments that passed the check. Commands run as the Pod's non-root UID;
Harbor's ``user`` argument cannot be honoured under ``restricted`` (no ``su``) and is ignored.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import shlex
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import urllib3
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.environments.tar_transfer import extract_dir
from harbor.models.task.config import TaskOS
from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

from . import harbor_binding
from .cluster import (
    APP_SA,
    EGRESS_CANARIES,
    ROLE_LABEL,
    SANDBOX_CONTAINER,
    TRIAL_LABEL,
    Names,
    Sandbox,
    _pod_security,
    baseline,
    exec_rbac,
)

LOG = logging.getLogger("bravo_rehearsal.harbor_env")
ENV_TYPE = "bravo-k8s-namespace"
VERIFIER_SESSION_MARK = "__verifier__"  # Harbor 0.25.0 trial.py _separate_verifier_session_id
VERIFY_EXEC_SA = "verify-exec"
VERIFIER_CONTAINER = "verifier"
ARTIFACT_PATH = "/input/artifact.json"
PROPOSAL_PATH = "/out/proposal.json"  # where platform/agent/harbor verifiers read the frozen artifact
VERIFIER_UPLOAD_ROOTS = ("/tests",)  # platform-held tests only; nothing from the sandbox (05 3 분리와 전달)
EXEC_TOKEN_TTL_S = 600
CHUNK = 57 * 1024  # base64 of 57 KiB = 76 KiB per stdin frame
WRITABLE_MOUNTS = ("/src", "/work", "/out", "/app", "/tmp", "/logs", "/tests", "/input")  # 13 4 roots + Harbor's


class EnvironmentError_(RuntimeError):
    """Base for environment failures. ``str(exc)`` is the Trial reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class IsolationCheckFailed(EnvironmentError_):
    pass


class DefinitionInvalid(ValueError):
    """Raised from ``_validate_definition`` (Harbor's contract: a ValueError). Message is the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TransferError(EnvironmentError_):
    pass


# Tar over exec: pure helpers (tested without a cluster) ------------------------------------------

# Runs inside the Pod: read base64 lines until "EOF", unpack with the data filter, print the size.
RECV_SCRIPT = (
    "import base64,io,os,sys,tarfile\n"
    "d=sys.argv[1];os.makedirs(d,exist_ok=True);buf=bytearray();done=False\n"
    "for line in sys.stdin.buffer:\n"
    "    line=line.strip()\n"
    "    if line==b'EOF':\n"
    "        done=True;break\n"
    "    buf+=base64.b64decode(line)\n"
    "if not done:\n"
    "    sys.exit('transfer_truncated')\n"
    "with tarfile.open(fileobj=io.BytesIO(bytes(buf)),mode='r:*') as t:\n"
    "    t.extractall(d,filter='data')\n"
    "print(len(buf))\n"
)

# Runs inside the Pod: pack a file (as member "payload") or a directory (rooted at "."), print base64 + EOF.
SEND_SCRIPT = (
    "import base64,io,os,sys,tarfile\n"
    "s,m=sys.argv[1],sys.argv[2]\n"
    "if (m=='dir' and not os.path.isdir(s)) or (m=='file' and not os.path.isfile(s)):\n"
    "    sys.exit('transfer_source_missing:'+s)\n"
    "b=io.BytesIO()\n"
    "with tarfile.open(fileobj=b,mode='w') as t:\n"
    "    t.add(s,arcname='.' if m=='dir' else 'payload',recursive=(m=='dir'))\n"
    "e=base64.b64encode(b.getvalue()).decode()\n"
    "for i in range(0,len(e),76*1024):\n"
    "    sys.stdout.write(e[i:i+76*1024]+'\\n')\n"
    "sys.stdout.write('EOF\\n')\n"
)


def pack_file(source: Path, arcname: str) -> bytes:
    if not source.is_file():
        raise FileNotFoundError(f"Source file {source} does not exist")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(source), arcname=arcname, recursive=False)
    return buf.getvalue()


def pack_dir(source: Path) -> bytes:
    if not source.is_dir():
        raise FileNotFoundError(f"Source directory {source} does not exist")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(source), arcname=".")
    return buf.getvalue()


def encode_frames(data: bytes) -> list[str]:
    """Base64 lines for stdin, each a complete chunk, ending with the ``EOF`` marker."""
    lines = [base64.b64encode(data[i:i + CHUNK]).decode() + "\n" for i in range(0, len(data), CHUNK)]
    return [*lines, "EOF\n"]


def decode_frames(text: str) -> bytes:
    """Inverse of the Pod's SEND_SCRIPT output. A missing ``EOF`` means the stream was cut."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[-1] != "EOF":
        raise TransferError("transfer_truncated")
    try:
        return b"".join(base64.b64decode(ln, validate=True) for ln in lines[:-1])
    except ValueError as exc:
        raise TransferError("transfer_corrupt") from exc


def unpack_file(archive: bytes, target: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
        member = tar.getmember("payload")
        if not member.isfile():
            raise TransferError("transfer_not_a_file")
        src = tar.extractfile(member)
        assert src is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read())


def unpack_dir(archive: bytes, target: Path) -> list[str]:
    """Unpack with Harbor's data-filter extractor; returns refused members (logged by caller)."""
    return extract_dir(io.BytesIO(archive), target)


def shell_command(command: str, cwd: str | None, env: dict[str, str] | None) -> list[str]:
    """argv for Harbor's command string: bash when the image has it (Harbor's scripts assume it), else sh.

    cwd and env are applied inside the Pod. The command itself is passed as ``$0`` of the
    launcher, so it is never re-quoted.
    """
    prefix = ""
    for key, value in (env or {}).items():
        if not key.isidentifier():
            raise ValueError(f"invalid environment variable name: {key!r}")
        prefix += f"export {key}={shlex.quote(value)}; "
    if cwd:
        prefix += f"cd {shlex.quote(cwd)} && "
    launcher = 'if command -v bash >/dev/null 2>&1; then exec bash -c "$0"; else exec sh -c "$0"; fi'
    return ["sh", "-c", launcher, prefix + command]


def is_verifier_session(session_id: str) -> bool:
    return VERIFIER_SESSION_MARK in session_id


def digest_pinned(image: str | None) -> bool:
    return bool(image) and "@sha256:" in str(image) and len(str(image).rsplit("@sha256:", 1)[1]) == 64


# Pod templates -----------------------------------------------------------------------------------


def harbor_pod(name: str, trial_id: str, image: str, role: str, container: str) -> dict[str, Any]:
    """Fixed template. emptyDirs for Harbor's paths; the root filesystem stays read-only."""
    mounts = [{"name": m.strip("/"), "mountPath": m} for m in WRITABLE_MOUNTS]
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "labels": {TRIAL_LABEL: trial_id, ROLE_LABEL: role}},
        "spec": {
            "serviceAccountName": APP_SA, "automountServiceAccountToken": False, "enableServiceLinks": False,
            "restartPolicy": "Never", "securityContext": _pod_security(),
            "containers": [{
                "name": container, "image": image, "imagePullPolicy": "IfNotPresent",
                "command": ["sleep", "infinity"],
                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                                    "readOnlyRootFilesystem": True, "runAsNonRoot": True},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                              "limits": {"cpu": "500m", "memory": "256Mi"}},
                "volumeMounts": mounts,
            }],
            "volumes": [{"name": m.strip("/"), "emptyDir": {"sizeLimit": "64Mi"}} for m in WRITABLE_MOUNTS],
        },
    }


def verify_exec_rbac(ns: str, trial_id: str, pod: str) -> list[tuple[str, dict[str, Any]]]:
    labels = {TRIAL_LABEL: trial_id, ROLE_LABEL: "verify"}
    sa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": VERIFY_EXEC_SA, "labels": labels},
          "automountServiceAccountToken": False}
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
            "metadata": {"name": VERIFY_EXEC_SA, "labels": labels},
            "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"], "resourceNames": [pod]},
                      {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create", "get"],
                       "resourceNames": [pod]}]}
    binding = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
               "metadata": {"name": VERIFY_EXEC_SA, "labels": labels},
               "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": VERIFY_EXEC_SA},
               "subjects": [{"kind": "ServiceAccount", "name": VERIFY_EXEC_SA, "namespace": ns}]}
    return [("sa", sa), ("role", role), ("binding", binding)]


@dataclass
class _Target:
    namespace: str
    pod: str
    container: str
    exec_sa: str
    pod_uid: str = ""
    image_id: str = ""
    exec_api: k8s.ApiClient | None = None
    exec_exp: float = 0.0
    refused_uploads: list[str] = field(default_factory=list)


class RehearsalNamespaceEnvironment(BaseEnvironment):
    """import_path: ``bravo_rehearsal.harbor_env:RehearsalNamespaceEnvironment``.

    Environment kwargs (``EnvironmentConfig.kwargs``, set by the rehearsal service only):
    ``kubeconfig``, ``kube_context``, ``trial_id``, ``plan_id``, ``attempt``, ``sandbox_image``,
    ``verifier_image`` (both digest-pinned), ``ready_timeout_sec``.
    """

    def __init__(self, *args: Any, kubeconfig: str | None = None, kube_context: str | None = None,
                 trial_id: str | None = None, plan_id: str | None = None, attempt: int | None = None,
                 sandbox_image: str | None = None, verifier_image: str | None = None,
                 ready_timeout_sec: int = 120, **kwargs: Any) -> None:
        self._kubeconfig = Path(kubeconfig).expanduser() if kubeconfig else None
        self._kube_context = kube_context
        self._trial_id = trial_id or ""
        self._plan_id = plan_id or ""
        self._attempt = int(attempt or 0)
        self._sandbox_image = sandbox_image
        self._verifier_image = verifier_image
        self._ready_timeout_sec = ready_timeout_sec
        self._api: k8s.ApiClient | None = None
        self._target: _Target | None = None
        self._owns_namespace = False  # set only once this environment created its namespace
        super().__init__(*args, **kwargs)

    # Identity and contract ---------------------------------------------------------------------

    @staticmethod
    def type() -> str:
        return ENV_TYPE

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        # disable_internet: default-deny NetworkPolicy, verified by the canaries in start().
        return EnvironmentCapabilities(disable_internet=True)

    @property
    def is_verifier(self) -> bool:
        return is_verifier_session(self.session_id)

    @property
    def names(self) -> Names:
        return Names.of(self._plan_id, self._attempt, self._trial_id)

    @property
    def namespace(self) -> str:
        return self.names.verify_ns if self.is_verifier else self.names.rehearsal_ns

    @property
    def pod_name(self) -> str:
        return f"verify-runner-{self._trial_id[:12]}" if self.is_verifier else self.names.sandbox_pod

    @property
    def image(self) -> str | None:
        """Verifier: the service's pinned verifier image. Agent: the task's role image when digest-pinned
        (``platform/agent/harbor/<role>`` templates are platform-owned), else the service's sandbox image."""
        if self.is_verifier:
            return self._verifier_image
        task_image = self.task_env_config.docker_image
        return task_image if digest_pinned(task_image) else self._sandbox_image

    @property
    def sandbox(self) -> Sandbox:
        """The tool layer's handle on this Trial's sandbox (agent environment only)."""
        if self.is_verifier or self._target is None or not self._target.pod_uid:
            raise EnvironmentError_("sandbox_unavailable")
        return Sandbox(names=self.names, pod_uid=self._target.pod_uid)

    @property
    def verifier_record(self) -> dict[str, Any]:
        t = self._target
        if not self.is_verifier or t is None or not t.pod_uid:
            raise EnvironmentError_("verifier_unbound")
        return {"namespace": t.namespace, "pod": t.pod, "pod_uid": t.pod_uid, "image_id": t.image_id,
                "refused_uploads": list(t.refused_uploads)}

    def _validate_definition(self) -> None:
        if not self.environment_dir.is_dir():
            raise FileNotFoundError(f"environment directory {self.environment_dir} not found")
        if (self.environment_dir / "docker-compose.yaml").exists():
            raise DefinitionInvalid("compose_unsupported")
        if self._host_mounts:  # nothing on the platform host is mounted into a rehearsal Pod
            raise DefinitionInvalid("host_mounts_unsupported")
        if self.task_env_config.os != TaskOS.LINUX:
            raise DefinitionInvalid("os_unsupported")
        if not (self._trial_id and self._plan_id and self._attempt >= 1):
            raise DefinitionInvalid("trial_binding_kwargs_missing")
        if self._kubeconfig is None or not self._kubeconfig.is_file():
            raise DefinitionInvalid("cluster_unconfigured")
        if not digest_pinned(self.image):
            raise DefinitionInvalid("verifier_unconfigured" if self.is_verifier else "sandbox_image_unpinned")

    # Cluster access ----------------------------------------------------------------------------

    def _client(self) -> k8s.ApiClient:
        if self._api is None:
            assert self._kubeconfig is not None
            self._api = k8s_config.new_client_from_config(config_file=str(self._kubeconfig),
                                                          context=self._kube_context)
        return self._api

    def _create(self, kind: str, body: dict[str, Any]) -> None:
        api, ns = self._client(), self.namespace
        {
            "np": lambda: k8s.NetworkingV1Api(api).create_namespaced_network_policy(ns, body),
            "quota": lambda: k8s.CoreV1Api(api).create_namespaced_resource_quota(ns, body),
            "limits": lambda: k8s.CoreV1Api(api).create_namespaced_limit_range(ns, body),
            "sa": lambda: k8s.CoreV1Api(api).create_namespaced_service_account(ns, body),
            "role": lambda: k8s.RbacAuthorizationV1Api(api).create_namespaced_role(ns, body),
            "binding": lambda: k8s.RbacAuthorizationV1Api(api).create_namespaced_role_binding(ns, body),
            "pod": lambda: k8s.CoreV1Api(api).create_namespaced_pod(ns, body),
        }[kind]()

    def _provision(self) -> _Target:
        role = "verify" if self.is_verifier else "rehearsal"
        labels = {TRIAL_LABEL: self._trial_id, ROLE_LABEL: role,
                  "pod-security.kubernetes.io/enforce": "restricted",
                  "pod-security.kubernetes.io/enforce-version": "latest",
                  "pod-security.kubernetes.io/warn": "restricted",
                  "bravo.dev/kyverno-baseline": "rehearsal"}
        core = k8s.CoreV1Api(self._client())
        try:
            core.create_namespace({"apiVersion": "v1", "kind": "Namespace",
                                   "metadata": {"name": self.namespace, "labels": labels}})
        except ApiException as exc:
            if exc.status == 409:
                raise EnvironmentError_("namespace_exists") from exc  # names are never reused (V3)
            raise
        self._owns_namespace = True  # from here on, stop() deletes it; never someone else's namespace
        for kind, body in baseline(self.namespace, self._trial_id, role):
            self._create(kind, body)
        if self.is_verifier:
            rbac, sa, container = verify_exec_rbac(self.namespace, self._trial_id, self.pod_name), VERIFY_EXEC_SA, \
                VERIFIER_CONTAINER
        else:
            rbac, sa, container = exec_rbac(self.namespace, self._trial_id, self.pod_name), "rehearsal-exec", \
                SANDBOX_CONTAINER
        for kind, body in rbac:
            self._create(kind, body)
        assert self.image is not None
        self._create("pod", harbor_pod(self.pod_name, self._trial_id, self.image,
                                       "verifier" if self.is_verifier else "sandbox", container))
        target = _Target(namespace=self.namespace, pod=self.pod_name, container=container, exec_sa=sa)
        deadline = time.monotonic() + self._ready_timeout_sec
        while time.monotonic() < deadline:
            p = core.read_namespaced_pod(self.pod_name, self.namespace)
            if p.status.phase == "Running":
                target.pod_uid = str(p.metadata.uid)
                statuses = p.status.container_statuses or []
                target.image_id = str(statuses[0].image_id) if statuses else ""
                return target
            if p.status.phase in ("Failed", "Succeeded"):
                raise EnvironmentError_(f"pod_{p.status.phase.lower()}")
            time.sleep(1)
        raise EnvironmentError_("pod_not_ready")

    def _exec_client(self, t: _Target) -> k8s.ApiClient:
        if t.exec_api is not None and t.exec_exp - time.time() > 60:
            return t.exec_api
        req = k8s.CoreV1Api(self._client()).create_namespaced_service_account_token(
            t.exec_sa, t.namespace, {"spec": {"expirationSeconds": EXEC_TOKEN_TTL_S}})
        base = self._client().configuration
        cfg = k8s.Configuration()
        cfg.host, cfg.ssl_ca_cert, cfg.verify_ssl = base.host, base.ssl_ca_cert, base.verify_ssl
        cfg.api_key = {"authorization": f"Bearer {req.status.token}"}
        t.exec_api, t.exec_exp = k8s.ApiClient(cfg), time.time() + EXEC_TOKEN_TTL_S
        return t.exec_api

    def _run(self, argv: list[str], stdin_frames: list[str] | None, timeout_s: float) -> tuple[int, str, str]:
        """One exec with the exec identity. Pod UID is re-read first (V3b)."""
        t = self._target
        if t is None:
            raise EnvironmentError_("environment_not_started")
        api = self._exec_client(t)
        core = k8s.CoreV1Api(api)
        if str(core.read_namespaced_pod(t.pod, t.namespace).metadata.uid) != t.pod_uid:
            raise EnvironmentError_("pod_uid_mismatch")
        ws = stream(core.connect_get_namespaced_pod_exec, t.pod, t.namespace, command=argv, container=t.container,
                    stderr=True, stdin=stdin_frames is not None, stdout=True, tty=False, _preload_content=False)
        out: list[str] = []
        err: list[str] = []
        try:
            for frame in stdin_frames or []:
                ws.write_stdin(frame)
            deadline = time.monotonic() + timeout_s
            while ws.is_open() and time.monotonic() < deadline:
                ws.update(timeout=1)
                if ws.peek_stdout():
                    out.append(ws.read_stdout())
                if ws.peek_stderr():
                    err.append(ws.read_stderr())
            timed_out = ws.is_open()
        finally:
            ws.close()
        if timed_out:
            return 124, "".join(out), "".join(err) + f"\ncommand timed out after {timeout_s} seconds"
        rc = ws.returncode
        if rc is None:
            raise EnvironmentError_("exec_stream_closed")
        return int(rc), "".join(out), "".join(err)

    async def _call(self, fn: Any, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, *args)
        except ApiException as exc:
            raise EnvironmentError_(f"k8s_{exc.status}") from exc
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise EnvironmentError_("cluster_unreachable") from exc

    # Harbor contract ---------------------------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        if force_build:
            self.logger.debug("force_build ignored: images are digest-pinned and never built here")
        self._target = await self._call(self._provision)
        problems = await self.isolation_check()  # both namespaces, before anything enters the Pod
        if problems:
            raise IsolationCheckFailed("isolation_check_failed:" + ",".join(problems))
        if self.is_verifier:
            await self._seed_artifact()
        else:
            # Working copies, seeded through the exec identity: the application and the source snapshot.
            for local, remote in (("app", "/app"), ("source", "/src")):
                if (self.environment_dir / local).is_dir():
                    await self.upload_dir(self.environment_dir / local, remote)
            binding = harbor_binding.get(self._trial_id)
            if binding is not None:
                binding.trial.box = self.sandbox

    async def isolation_check(self) -> list[str]:
        """T3 right after creation. Control first, so a failed connect counts as blocked."""
        ctl = await self.exec("python3 -I -c 'print(1)'", timeout_sec=30)
        if ctl.return_code != 0:
            return ["control_exec_failed"]
        problems = []
        for host, port in EGRESS_CANARIES:
            code = f"import socket; socket.create_connection(({host!r}, {port}), 3)"
            r = await self.exec(f"python3 -I -c {shlex.quote(code)}", timeout_sec=15)
            if r.return_code == 0:
                problems.append(f"egress_open:{host}:{port}")
        priv = harbor_pod("bravo-psa-probe", "probe", "registry.invalid/probe@sha256:" + "0" * 64, "probe", "probe")
        priv["spec"]["containers"][0]["securityContext"] = {"privileged": True}

        def probe() -> str | None:
            try:
                k8s.CoreV1Api(self._client()).create_namespaced_pod(self.namespace, priv, dry_run="All")
            except ApiException as exc:
                return None if exc.status in (400, 403, 422) else f"psa_probe_error:{exc.status}"
            return "privileged_pod_admitted"

        p = await asyncio.to_thread(probe)
        return problems + ([p] if p else [])

    async def _seed_artifact(self) -> None:
        """The frozen ``plan_propose`` artifact goes platform → verifier, never sandbox → verifier."""
        binding = harbor_binding.get(self._trial_id)
        if binding is None or binding.trial.artifact is None:
            raise EnvironmentError_("no_frozen_artifact")
        raw = harbor_binding.canonical(binding.trial.artifact)
        with tempfile.TemporaryDirectory(prefix="bravo-verify-") as tmp:
            src = Path(tmp) / "artifact.json"
            src.write_bytes(raw)
            for target in (ARTIFACT_PATH, PROPOSAL_PATH):
                path = PurePosixPath(target)
                await self._upload(pack_file(src, path.name), str(path.parent))

    async def stop(self, delete: bool) -> None:
        if not self._owns_namespace:
            return
        binding = harbor_binding.get(self._trial_id)
        if self.is_verifier and binding is not None and self._target is not None and self._target.pod_uid:
            binding.verifier = self.verifier_record
        if not delete:
            self.logger.warning("namespace %s kept (delete=false)", self.namespace)
            return
        left = await asyncio.to_thread(self._delete_namespace, 120)
        if left and binding is not None:
            binding.trial.leftovers.extend(left)

    def _delete_namespace(self, timeout_s: int) -> list[str]:
        core = k8s.CoreV1Api(self._client())
        try:
            core.delete_namespace(self.namespace, propagation_policy="Foreground")
        except ApiException as exc:
            if exc.status == 404:
                return []
            LOG.warning("namespace delete failed ns=%s status=%s", self.namespace, exc.status)
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                core.read_namespace(self.namespace)
            except ApiException as exc:
                if exc.status == 404:
                    return []
            time.sleep(2)
        return [self.namespace]  # V6: recorded on the Trial as a leftover

    async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
                   timeout_sec: int | None = None, user: str | int | None = None) -> ExecResult:
        if user is not None:
            self.logger.debug("exec user=%r ignored: restricted Pods run as their non-root UID", user)
        argv = shell_command(command, cwd or self.task_env_config.workdir, self._merge_env(env))
        rc, out, err = await self._call(self._run, argv, None, float(timeout_sec or 600))
        return ExecResult(stdout=out, stderr=err, return_code=rc)

    def _upload_allowed(self, target: str) -> bool:
        if not self.is_verifier:
            return True
        p = PurePosixPath(target)
        ok = any(p == PurePosixPath(r) or PurePosixPath(r) in p.parents for r in VERIFIER_UPLOAD_ROOTS)
        if not ok and self._target is not None:
            self._target.refused_uploads.append(target)
            self.logger.warning("verifier upload to %s refused: only platform-held tests enter the verifier", target)
        return ok

    async def _upload(self, archive: bytes, target_dir: str) -> None:
        argv = ["python3", "-I", "-c", RECV_SCRIPT, target_dir]
        rc, out, err = await self._call(self._run, argv, encode_frames(archive), 600.0)
        if rc != 0 or out.strip().splitlines()[-1:] != [str(len(archive))]:
            raise TransferError(f"upload_failed:{rc}:{err.strip()[:200]}")

    async def _download(self, source: str, mode: str) -> bytes:
        argv = ["python3", "-I", "-c", SEND_SCRIPT, source, mode]
        rc, out, err = await self._call(self._run, argv, None, 600.0)
        if rc != 0:
            raise TransferError(f"download_failed:{rc}:{err.strip()[:200]}")
        return decode_frames(out)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        if not self._upload_allowed(target_path):
            return
        target = PurePosixPath(target_path)
        await self._upload(pack_file(Path(source_path), target.name), str(target.parent))

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        if not self._upload_allowed(target_dir):
            return
        await self._upload(pack_dir(Path(source_dir)), target_dir)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        unpack_file(await self._download(source_path, "file"), Path(target_path))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        refused = unpack_dir(await self._download(source_dir, "dir"), Path(target_dir))
        if refused:
            self.logger.warning("download %s: %d unsafe members skipped, e.g. %s", source_dir, len(refused), refused[0])
