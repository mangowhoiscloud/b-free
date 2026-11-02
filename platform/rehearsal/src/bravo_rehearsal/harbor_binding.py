"""In-process link between the rehearsal service and the Harbor objects of one Trial.

Harbor builds the environments and the agent itself from import paths and kwargs, so they
cannot receive Python objects. The service registers one ``Binding`` per running Trial,
keyed by Trial ID, before ``harbor.trial.Trial.create``; the environment and the agent look it
up with the ``trial_id`` kwarg. The binding carries only platform-side state: the service's
Trial record (tool-call log, frozen artifact, verdict CAS), the worker environment factory
(holds the Trial token), and what the verifier environment observed from the Kubernetes API
(namespace, Pod UID, image ID). Nothing here is reachable from the sandbox or the agent worker.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .cluster import Cluster
    from .trials import Trial


def canonical(artifact: Any) -> bytes:
    """Bytes the verifier receives. sha256 of these equals ``proposal.digest`` (same canonical form)."""
    return json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass
class Binding:
    trial: Trial
    worker_env: Callable[[str], dict[str, str]]  # config dir -> clean environment for the worker
    cluster: Cluster | None = None  # tool-layer backend; applies the frozen manifest after the worker stops
    verifier: dict[str, Any] | None = None  # filled by the verify-namespace environment on stop


_lock = threading.Lock()
_bindings: dict[str, Binding] = {}


def register(binding: Binding) -> None:
    with _lock:
        if binding.trial.trial_id in _bindings:
            raise ValueError("trial_already_bound")
        _bindings[binding.trial.trial_id] = binding


def get(trial_id: str) -> Binding | None:
    with _lock:
        return _bindings.get(trial_id)


def release(trial_id: str) -> None:
    with _lock:
        _bindings.pop(trial_id, None)
