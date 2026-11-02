# app-migration-agent sandbox.
# Build context: platform/agent
#   docker build -f images/app-migration-agent/Dockerfile -t bravo/agent-sandbox-app-migration-agent:dev .
# Final: python:3.12.16-slim-trixie, hardened (no apt/dpkg/pip/perl/bash, no setuid), + rg + jq.
# Debian diffutils `diff` comes from the base. /bin/sh is dash (the only shell).

# ---- fetch: downloads run on the build host; every artifact pinned by sha256 ----
FROM --platform=$BUILDPLATFORM busybox:1.38.0-musl@sha256:80e38f9a1b39188b8bdb28879178edb322a4402b081a9aedda1924609decd6f6 AS fetch
ARG TARGETARCH

# jq 1.8.2 — checksums: https://github.com/jqlang/jq/releases/download/jq-1.8.2/sha256sum.txt
ADD --checksum=sha256:b1c22172dd303f3be49e935aa56aa48a8b7a46e0bc838b4997d3bb451495870f \
    https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-linux-amd64 /dl/amd64/jq
ADD --checksum=sha256:8b85c817833814ddca00a144c33705546355afccf0cf39b188f3cdb48b852309 \
    https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-linux-arm64 /dl/arm64/jq

# ripgrep 15.2.0 (static musl) — checksums: per-asset .sha256 files, e.g.
# https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz.sha256
ADD --checksum=sha256:33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c \
    https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz /dl/amd64/ripgrep.tar.gz
ADD --checksum=sha256:800b1e7206afe799dfb5a6901f23147cfaabe0e52210538100f61e86e1740915 \
    https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-aarch64-unknown-linux-musl.tar.gz /dl/arm64/ripgrep.tar.gz

RUN set -eu; \
    case "$TARGETARCH" in amd64|arm64) ;; *) echo "unsupported TARGETARCH=$TARGETARCH" >&2; exit 1 ;; esac; \
    mkdir -p /stage/bin /tmp/x; d="/dl/$TARGETARCH"; \
    install -m 0755 "$d/jq" /stage/bin/jq; \
    tar -xzf "$d/ripgrep.tar.gz" -C /tmp/x; install -m 0755 /tmp/x/ripgrep-15.2.0-*/rg /stage/bin/rg

# ---- final ----
FROM python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07

COPY --from=fetch /stage/bin/ /usr/local/bin/

USER 0:0
RUN --mount=type=bind,source=images/common/harden-debian.sh,target=/tmp/harden-debian.sh \
    set -eu; \
    install -d -o 65532 -g 65532 -m 0755 /work /out; \
    install -d -o 0 -g 0 -m 0555 /src; \
    sh /tmp/harden-debian.sh

LABEL org.opencontainers.image.title="bravo-agent-sandbox-app-migration-agent" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.source="https://github.com/team-bravo/bravo-platform" \
      io.bravo.agent-role="app-migration-agent"

# TMPDIR points at writable scratch so tools work on a read-only root filesystem.
ENV HOME=/work \
    TMPDIR=/work \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /work
USER 65532:65532
ENTRYPOINT []
# Keepalive so the harness can exec into the running container.
CMD ["/usr/bin/sleep", "infinity"]
