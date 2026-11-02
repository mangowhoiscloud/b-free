# ci-agent sandbox.
# Build context: platform/agent (needs templates/bravo-app/)
#   docker build -f images/ci-agent/Dockerfile -t bravo/agent-sandbox-ci-agent:dev .
# Final: python:3.12.16-slim-trixie, hardened (no apt/dpkg/pip/perl/bash, no setuid)
#        + pytest (PYTHONPATH=/opt/bravo/pylib; run as `python3 -m pytest`)
#        + helm, hadolint, kubeconform, syft, trivy, jq + offline schemas + bravo-app chart.
#
# Offline kubeconform:
#   kubeconform -strict -summary \
#     -schema-location '/opt/bravo/schemas/{{.ResourceKind}}{{.KindSuffix}}.json' \
#     -schema-location '/opt/bravo/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' <files>
# Offline trivy (verified with --network none --read-only; uses the checks bundle embedded in the binary):
#   trivy config --skip-check-update <dir>      (trivy v0.76.1 `config` rejects --offline-scan: "unknown flag")
#   trivy fs --scanners misconfig --skip-db-update --skip-check-update --offline-scan <dir>
# Vulnerability scanning (trivy fs/image --scanners vuln) needs a vuln DB and is NOT available offline.
# Schema sources (no published checksum files; sha256 computed by us on 2026-11-02 from the raw files):
#   Kubernetes v1.35.10 (newest v1.35.x) from yannh/kubernetes-json-schema @ e0c72a1bc5f409d1685c0e1323460c8a98cd7459
#   (dir v1.35.10-standalone-strict); external-secrets.io/v1 from datreeio/CRDs-catalog @ 27233a506de0085e71c7f9702597806a7f95eb24.

# ---- fetch: downloads run on the build host; every artifact pinned by sha256 ----
FROM --platform=$BUILDPLATFORM busybox:1.38.0-musl@sha256:80e38f9a1b39188b8bdb28879178edb322a4402b081a9aedda1924609decd6f6 AS fetch
ARG TARGETARCH

# jq 1.8.2 — checksums: https://github.com/jqlang/jq/releases/download/jq-1.8.2/sha256sum.txt
ADD --checksum=sha256:b1c22172dd303f3be49e935aa56aa48a8b7a46e0bc838b4997d3bb451495870f \
    https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-linux-amd64 /dl/amd64/jq
ADD --checksum=sha256:8b85c817833814ddca00a144c33705546355afccf0cf39b188f3cdb48b852309 \
    https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-linux-arm64 /dl/arm64/jq

# helm v4.3.1 — checksums: https://get.helm.sh/helm-v4.3.1-linux-amd64.tar.gz.sha256sum (and -arm64)
ADD --checksum=sha256:0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b \
    https://get.helm.sh/helm-v4.3.1-linux-amd64.tar.gz /dl/amd64/helm.tar.gz
ADD --checksum=sha256:8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88 \
    https://get.helm.sh/helm-v4.3.1-linux-arm64.tar.gz /dl/arm64/helm.tar.gz

# hadolint v2.15.1 — the project publishes NO checksum file for these assets;
# sha256 computed by us on 2026-11-02 from the release assets.
ADD --checksum=sha256:c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507 \
    https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-linux-x86_64 /dl/amd64/hadolint
ADD --checksum=sha256:f6198ef8090f404dbb771abfee086eb8c48ac177f30da7fd3510aca35b344b5d \
    https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-linux-arm64 /dl/arm64/hadolint

# kubeconform v0.8.0 — checksums: https://github.com/yannh/kubeconform/releases/download/v0.8.0/CHECKSUMS
ADD --checksum=sha256:9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883 \
    https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-amd64.tar.gz /dl/amd64/kubeconform.tar.gz
ADD --checksum=sha256:1f53fc8e81258197a35e8603054162a5af1de8c5af13746c71ab680d9534ed87 \
    https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-arm64.tar.gz /dl/arm64/kubeconform.tar.gz

# syft v1.55.0 — checksums: https://github.com/anchore/syft/releases/download/v1.55.0/syft_1.55.0_checksums.txt
ADD --checksum=sha256:bb65249542db5fedeeb4d3f9b018b75e7264dbfbbb10486f044581a92f90490d \
    https://github.com/anchore/syft/releases/download/v1.55.0/syft_1.55.0_linux_amd64.tar.gz /dl/amd64/syft.tar.gz
ADD --checksum=sha256:929bd163db2d7cd2255d3bcb8967208079813d7b46b0c952f7b794d8e2c7d304 \
    https://github.com/anchore/syft/releases/download/v1.55.0/syft_1.55.0_linux_arm64.tar.gz /dl/arm64/syft.tar.gz

# trivy v0.76.1 — checksums: https://github.com/aquasecurity/trivy/releases/download/v0.76.1/trivy_0.76.1_checksums.txt
ADD --checksum=sha256:c7cbe3137fb8a48a17574bede3dd6beb16742d1e9772929940fc90082c39d9c0 \
    https://github.com/aquasecurity/trivy/releases/download/v0.76.1/trivy_0.76.1_Linux-64bit.tar.gz /dl/amd64/trivy.tar.gz
ADD --checksum=sha256:e174dff2a6dba5e3ae41f8d0537a6324405bd08132497340304c6b20e0f16a1f \
    https://github.com/aquasecurity/trivy/releases/download/v0.76.1/trivy_0.76.1_Linux-ARM64.tar.gz /dl/arm64/trivy.tar.gz

# Kubernetes v1.35.10 standalone-strict schemas (sha256 computed by us, see header).
ADD --checksum=sha256:e0eaddebd677c08aa092b2da2264d86ac4fc34eed112b9fac2945b3f00c1e9b1 \
    https://raw.githubusercontent.com/yannh/kubernetes-json-schema/e0c72a1bc5f409d1685c0e1323460c8a98cd7459/v1.35.10-standalone-strict/configmap-v1.json /stage/schemas/configmap-v1.json
ADD --checksum=sha256:8193d6c3561475c6d3d5c44e1faedb1df53905373d904bc17015694326d659cf \
    https://raw.githubusercontent.com/yannh/kubernetes-json-schema/e0c72a1bc5f409d1685c0e1323460c8a98cd7459/v1.35.10-standalone-strict/serviceaccount-v1.json /stage/schemas/serviceaccount-v1.json
ADD --checksum=sha256:dfe8fb03b1642f985b57b12edd1727b6867c13ed4b6761ef81bdd51b1d236711 \
    https://raw.githubusercontent.com/yannh/kubernetes-json-schema/e0c72a1bc5f409d1685c0e1323460c8a98cd7459/v1.35.10-standalone-strict/role-rbac-v1.json /stage/schemas/role-rbac-v1.json
ADD --checksum=sha256:87ab620d0adddcd2803b92357fce2854af752326df8c5408a7588aae98b3031d \
    https://raw.githubusercontent.com/yannh/kubernetes-json-schema/e0c72a1bc5f409d1685c0e1323460c8a98cd7459/v1.35.10-standalone-strict/rolebinding-rbac-v1.json /stage/schemas/rolebinding-rbac-v1.json
ADD --checksum=sha256:f6324cc464f62228b0418f438d167208e4f86c7e3677ba30f608e79a8b26ba79 \
    https://raw.githubusercontent.com/yannh/kubernetes-json-schema/e0c72a1bc5f409d1685c0e1323460c8a98cd7459/v1.35.10-standalone-strict/networkpolicy-networking-v1.json /stage/schemas/networkpolicy-networking-v1.json
ADD --checksum=sha256:de761ccb06bd1d2c641fb57397f61b68e7ff53a384f0817ce40e5ded8cee86f3 \
    https://raw.githubusercontent.com/yannh/kubernetes-json-schema/e0c72a1bc5f409d1685c0e1323460c8a98cd7459/v1.35.10-standalone-strict/secret-v1.json /stage/schemas/secret-v1.json
ADD --checksum=sha256:46dc7cc4bec2c9a62400491e2b8057635afbfa6838999c518f7bea5c621a989b \
    https://raw.githubusercontent.com/yannh/kubernetes-json-schema/e0c72a1bc5f409d1685c0e1323460c8a98cd7459/v1.35.10-standalone-strict/deployment-apps-v1.json /stage/schemas/deployment-apps-v1.json
ADD --checksum=sha256:8bf019854daed511e7c174896a898173fa65d88ec5937c687a37303d4cc9351b \
    https://raw.githubusercontent.com/yannh/kubernetes-json-schema/e0c72a1bc5f409d1685c0e1323460c8a98cd7459/v1.35.10-standalone-strict/service-v1.json /stage/schemas/service-v1.json

# External Secrets CRDs, external-secrets.io/v1 (sha256 computed by us, see header).
ADD --checksum=sha256:7634aa711997c50e952970bc149fad4b2ac9e1fbc90047bc43e28e8c2682d570 \
    https://raw.githubusercontent.com/datreeio/CRDs-catalog/27233a506de0085e71c7f9702597806a7f95eb24/external-secrets.io/externalsecret_v1.json /stage/schemas/external-secrets.io/externalsecret_v1.json
ADD --checksum=sha256:94db482fe614022da3c4280e18b3e5af3dec1dc90359a507b8d027306ebab012 \
    https://raw.githubusercontent.com/datreeio/CRDs-catalog/27233a506de0085e71c7f9702597806a7f95eb24/external-secrets.io/secretstore_v1.json /stage/schemas/external-secrets.io/secretstore_v1.json

RUN set -eu; \
    case "$TARGETARCH" in amd64|arm64) ;; *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 1 ;; esac; \
    mkdir -p /stage/bin /tmp/x; d="/dl/$TARGETARCH"; \
    install -m 0755 "$d/jq" /stage/bin/jq; \
    install -m 0755 "$d/hadolint" /stage/bin/hadolint; \
    tar -xzf "$d/helm.tar.gz" -C /tmp/x; install -m 0755 "/tmp/x/linux-$TARGETARCH/helm" /stage/bin/helm; \
    tar -xzf "$d/kubeconform.tar.gz" -C /tmp/x kubeconform; install -m 0755 /tmp/x/kubeconform /stage/bin/kubeconform; \
    tar -xzf "$d/syft.tar.gz" -C /tmp/x syft; install -m 0755 /tmp/x/syft /stage/bin/syft; \
    tar -xzf "$d/trivy.tar.gz" -C /tmp/x trivy; install -m 0755 /tmp/x/trivy /stage/bin/trivy; \
    chown -R 0:0 /stage/schemas; find /stage/schemas -type d -exec chmod 0555 {} +; find /stage/schemas -type f -exec chmod 0444 {} +

# ---- pylib: pytest + deps from hash-locked pure-python wheels (pip only exists in this stage) ----
FROM python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS pylib
WORKDIR /build
RUN --mount=type=bind,source=images/ci-agent/requirements.lock,target=/build/images/ci-agent/requirements.lock \
    set -eu; \
    pip install --no-cache-dir --disable-pip-version-check --no-input \
        --require-hashes --no-deps --only-binary=:all: \
        --target /opt/bravo/pylib -r images/ci-agent/requirements.lock; \
    rm -rf /opt/bravo/pylib/bin; \
    python3 -m compileall -q /opt/bravo/pylib

# ---- final ----
FROM python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07

COPY --from=fetch /stage/bin/ /usr/local/bin/
COPY --from=fetch /stage/schemas/ /opt/bravo/schemas/
COPY --from=pylib /opt/bravo/pylib/ /opt/bravo/pylib/
COPY templates/bravo-app/ /opt/bravo/charts/bravo-app/

USER 0:0
RUN --mount=type=bind,source=images/common/harden-debian.sh,target=/tmp/harden-debian.sh \
    set -eu; \
    install -d -o 65532 -g 65532 -m 0755 /work /out; \
    install -d -o 0 -g 0 -m 0555 /src; \
    chown -R 0:0 /opt/bravo; chmod -R a+rX,go-w /opt/bravo; \
    sh /tmp/harden-debian.sh

LABEL org.opencontainers.image.title="bravo-agent-sandbox-ci-agent" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.source="https://github.com/team-bravo/bravo-platform" \
      io.bravo.agent-role="ci-agent"

# TMPDIR points at writable scratch so tools work on a read-only root filesystem.
ENV HOME=/work \
    TMPDIR=/work \
    PYTHONPATH=/opt/bravo/pylib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /work
USER 65532:65532
ENTRYPOINT []
# Keepalive so the harness can exec into the running container.
CMD ["/usr/bin/sleep", "infinity"]
