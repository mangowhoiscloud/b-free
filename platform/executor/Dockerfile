# Bravo Cutover Executor. Base image pinned by digest (python:3.12-slim).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION=0.10.2
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# kubectl for the PgBouncer target switch (pods/exec, CLOUD-E2E-CONTRACT.md 9). Pinned by version
# and sha256 (same pin as deploy/platform/images/link). EKS auth: python -m bravo_executor.ekstoken.
FROM ${PYTHON_IMAGE} AS kubectl
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.36.6
ARG KUBECTL_SHA256_arm64=35507ef408c3d38849e5d3203e18f8e861db822aa664101a9ced0869edd06046
ARG KUBECTL_SHA256_amd64=29818b6729dc9262b692307470d0558d027725e738be5dd47c760261868a2634
RUN python3 - <<PY
import hashlib, urllib.request
arch = "${TARGETARCH}"
want = {"arm64": "${KUBECTL_SHA256_arm64}", "amd64": "${KUBECTL_SHA256_amd64}"}[arch]
data = urllib.request.urlopen(f"https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/{arch}/kubectl", timeout=120).read()
got = hashlib.sha256(data).hexdigest()
assert got == want, f"kubectl sha256 mismatch: {got}"
open("/kubectl", "wb").write(data)
PY
RUN chmod 0755 /kubectl

FROM ${PYTHON_IMAGE}
ARG APP_UID=10001
RUN groupadd --system --gid "${APP_UID}" bravo \
 && useradd --system --uid "${APP_UID}" --gid "${APP_UID}" --home-dir /app --no-create-home --shell /usr/sbin/nologin bravo
WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --from=kubectl --chown=root:root /kubectl /usr/local/bin/kubectl
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER bravo:bravo
EXPOSE 8081
ENTRYPOINT ["python", "-m", "bravo_executor"]
