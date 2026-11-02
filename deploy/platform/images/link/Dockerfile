# Platform link image (임시 링크(D-16 전), CLOUD-E2E-CONTRACT.md 7): tools/link (bravo-link) + kubectl + EKS token.
# - relay mode: the reverse-tunnel relay (tools/link relay.py) for platform -> on-prem K3s.
# - forward mode: a supervised `kubectl port-forward` platform pod -> source EKS service, authenticated
#   with an EKS bearer token signed from the pod's EKS Pod Identity credentials (no static key, NFR-6).
# Build (repository root):
#   docker build -f deploy/platform/images/link/Dockerfile \
#     --build-context extra=deploy/platform/images/link tools/link
ARG PYTHON_IMAGE=python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION=0.10.2
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
 && uv pip install --python /app/.venv/bin/python "botocore==1.43.123"

FROM ${PYTHON_IMAGE} AS kubectl
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.36.6
ARG KUBECTL_SHA256_arm64=35507ef408c3d38849e5d3203e18f8e861db822aa664101a9ced0869edd06046
ARG KUBECTL_SHA256_amd64=29818b6729dc9262b692307470d0558d027725e738be5dd47c760261868a2634
RUN python3 - <<PY
import hashlib, urllib.request
arch = "${TARGETARCH}"
want = {"arm64": "${KUBECTL_SHA256_arm64}", "amd64": "${KUBECTL_SHA256_amd64}"}[arch]
url = f"https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/{arch}/kubectl"
data = urllib.request.urlopen(url, timeout=120).read()
got = hashlib.sha256(data).hexdigest()
assert got == want, f"kubectl sha256 mismatch: {got}"
open("/kubectl", "wb").write(data)
PY
RUN chmod 0755 /kubectl

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 link && useradd --system --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin link
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --from=kubectl /kubectl /usr/local/bin/kubectl
COPY --from=build --chown=root:root /app/src/bravo_link/relay.py /opt/bravo-link/relay.py
COPY --from=extra --chmod=0755 eks-token.py /usr/local/bin/eks-token
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["bravo-link"]
