# governance-agent sandbox.
# Build context: platform/agent
#   docker build -f images/governance-agent/Dockerfile -t bravo/agent-sandbox-governance-agent:dev .
# Final: distroless static (nonroot) + static busybox (applets: sh cat ls base64 diff) + jq + kubeconform
#        + offline JSON schemas under /opt/bravo/schemas.
#
# Offline validation (verified inside the built image with --network none):
#   kubeconform -strict -summary \
#     -schema-location '/opt/bravo/schemas/{{.ResourceKind}}{{.KindSuffix}}.json' \
#     -schema-location '/opt/bravo/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
#     <files>
# Schema sources:
#   Kubernetes v1.35.10 (newest v1.35.x; v1.37.2 is the newest overall) from
#   yannh/kubernetes-json-schema @ e0c72a1bc5f409d1685c0e1323460c8a98cd7459, dir v1.35.10-standalone-strict.
#   External Secrets external-secrets.io/v1 from datreeio/CRDs-catalog @ 27233a506de0085e71c7f9702597806a7f95eb24.
#   Neither repo publishes checksum files: the sha256 values below were computed by us on 2026-11-02
#   from the raw files at those commits.

# ---- fetch: downloads run on the build host; every artifact pinned by sha256 ----
FROM --platform=$BUILDPLATFORM busybox:1.38.0-musl@sha256:80e38f9a1b39188b8bdb28879178edb322a4402b081a9aedda1924609decd6f6 AS fetch
ARG TARGETARCH

# jq 1.8.2 — checksums: https://github.com/jqlang/jq/releases/download/jq-1.8.2/sha256sum.txt
ADD --checksum=sha256:b1c22172dd303f3be49e935aa56aa48a8b7a46e0bc838b4997d3bb451495870f \
    https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-linux-amd64 /dl/amd64/jq
ADD --checksum=sha256:8b85c817833814ddca00a144c33705546355afccf0cf39b188f3cdb48b852309 \
    https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-linux-arm64 /dl/arm64/jq

# kubeconform v0.8.0 — checksums: https://github.com/yannh/kubeconform/releases/download/v0.8.0/CHECKSUMS
ADD --checksum=sha256:9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883 \
    https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-amd64.tar.gz /dl/amd64/kubeconform.tar.gz
ADD --checksum=sha256:1f53fc8e81258197a35e8603054162a5af1de8c5af13746c71ab680d9534ed87 \
    https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-arm64.tar.gz /dl/arm64/kubeconform.tar.gz

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

# External Secrets CRDs, external-secrets.io/v1 (sha256 computed by us, see header).
ADD --checksum=sha256:7634aa711997c50e952970bc149fad4b2ac9e1fbc90047bc43e28e8c2682d570 \
    https://raw.githubusercontent.com/datreeio/CRDs-catalog/27233a506de0085e71c7f9702597806a7f95eb24/external-secrets.io/externalsecret_v1.json /stage/schemas/external-secrets.io/externalsecret_v1.json
ADD --checksum=sha256:94db482fe614022da3c4280e18b3e5af3dec1dc90359a507b8d027306ebab012 \
    https://raw.githubusercontent.com/datreeio/CRDs-catalog/27233a506de0085e71c7f9702597806a7f95eb24/external-secrets.io/secretstore_v1.json /stage/schemas/external-secrets.io/secretstore_v1.json

RUN set -eu; \
    case "$TARGETARCH" in amd64|arm64) ;; *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 1 ;; esac; \
    mkdir -p /stage/bin /tmp/kc; d="/dl/$TARGETARCH"; \
    install -m 0755 "$d/jq" /stage/bin/jq; \
    tar -xzf "$d/kubeconform.tar.gz" -C /tmp/kc kubeconform; install -m 0755 /tmp/kc/kubeconform /stage/bin/kubeconform; \
    chown -R 0:0 /stage/schemas; find /stage/schemas -type d -exec chmod 0555 {} +; find /stage/schemas -type f -exec chmod 0444 {} +

# ---- static busybox for the target arch ----
FROM busybox:1.38.0-musl@sha256:80e38f9a1b39188b8bdb28879178edb322a4402b081a9aedda1924609decd6f6 AS busybox

# ---- final ----
FROM gcr.io/distroless/static-debian13:nonroot@sha256:25ac8f0c3b2334a9eb05452266bd4697864a8c8ef0ef71e5e6480d7f92868329

COPY --from=busybox /bin/busybox /usr/local/bin/busybox
COPY --from=fetch /stage/bin/ /usr/local/bin/
COPY --from=fetch /stage/schemas/ /opt/bravo/schemas/

USER 0:0
# Only the listed applets get symlinks. /bin -> usr/bin in this base, so /bin/sh == /usr/bin/sh.
RUN ["/usr/local/bin/busybox", "sh", "-euc", "B=/usr/local/bin/busybox; \
     $B ln -s /usr/local/bin/busybox /usr/bin/sh; \
     for a in cat ls base64 diff; do $B ln -s busybox /usr/local/bin/$a; done; \
     $B mkdir -p /work /out /src; \
     $B chown 65532:65532 /work /out; $B chmod 0755 /work /out; \
     $B chown 0:0 /src; $B chmod 0555 /src"]

LABEL org.opencontainers.image.title="bravo-agent-sandbox-governance-agent" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.source="https://github.com/team-bravo/bravo-platform" \
      io.bravo.agent-role="governance-agent"

# TMPDIR points at writable scratch so tools work on a read-only root filesystem.
ENV HOME=/work \
    TMPDIR=/work
WORKDIR /work
USER 65532:65532
ENTRYPOINT []
# Keepalive so the harness can exec into the running container. No `sleep` applet link exists.
CMD ["/usr/local/bin/busybox", "sleep", "infinity"]
