"""Temporary link between the platform laptop and the clusters (CLOUD-E2E-CONTRACT.md §7).

Label in every record: ``임시 링크(D-16 전)``. The target design (D-16) is Cloudflare Tunnel
with Access service tokens; until Zero Trust is enabled, the laptop is the only side that can
open connections:

* ``forward``: supervise ``kubectl port-forward`` from the laptop to a cluster Service or Pod
  (EKS DB for the forward subscription in K3s, which reaches the laptop as host.lima.internal).
* ``relay`` + ``reverse``: a reverse TCP tunnel so that a client inside EKS (the reverse
  subscription on the source DB, the source PgBouncer after cutover) reaches a service on the
  K3s side. The relay pod accepts in-cluster clients; the laptop agent holds idle connections
  to the relay through ``kubectl port-forward`` and splices each paired one to the target.
"""

LINK_LABEL = "임시 링크(D-16 전)"
__version__ = "0.1.0"
