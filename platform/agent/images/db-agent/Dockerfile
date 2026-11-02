# DB rehearsal runner image (PostgreSQL client tools only).
# D-26 (2026-11-02): this image is NOT the db-agent's sandbox. The platform tool layer runs it server-side for
# db_inspect / db_rehearse with credentials fetched from OpenBao at call time; results only go back to the model.
# The db-agent sandbox uses the governance-agent image (file operations + jq) and has no DB route or credential.
# Build context: platform/agent
#   docker build -f images/db-agent/Dockerfile -t bravo/agent-sandbox-db-agent:dev .
# Final: postgres:16.15-trixie (newest 16.x patch on 2026-11-02), stripped to the client tools
#        psql, pg_dump, pg_restore, pg_isready; no server binaries, no gosu, no entrypoint scripts;
#        hardened (no apt/dpkg/pip/perl/bash, no setuid).
# Inherited base metadata that a Dockerfile cannot remove: VOLUME /var/lib/postgresql/data, EXPOSE 5432.

FROM postgres:16.15-trixie@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a

USER 0:0
RUN --mount=type=bind,source=images/common/harden-debian.sh,target=/tmp/harden-debian.sh \
    set -eu; \
    PGBIN="/usr/lib/postgresql/$PG_MAJOR/bin"; \
    for f in "$PGBIN"/*; do \
      case "${f##*/}" in psql|pg_dump|pg_restore|pg_isready) ;; *) rm -rf "$f" ;; esac; \
    done; \
    rm -f /usr/local/bin/gosu /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-ensure-initdb.sh \
          /docker-entrypoint.sh; \
    rm -rf /docker-entrypoint-initdb.d "/usr/lib/postgresql/$PG_MAJOR/lib"; \
    # pg_config wrapper points at the removed server-side pg_config; gnupg only served apt key handling.
    rm -f /usr/bin/pg_config /usr/bin/gpg /usr/bin/gpg-* /usr/bin/gpgconf /usr/bin/gpgsm /usr/bin/gpgv /usr/bin/dirmngr*; \
    sh /tmp/harden-debian.sh; \
    for t in psql pg_dump pg_restore pg_isready; do ln -sf "$PGBIN/$t" "/usr/local/bin/$t"; done; \
    install -d -o 65532 -g 65532 -m 0755 /work /out; \
    install -d -o 0 -g 0 -m 0555 /src

LABEL org.opencontainers.image.title="bravo-agent-sandbox-db-agent" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.source="https://github.com/team-bravo/bravo-platform" \
      io.bravo.agent-role="db-agent"

# Client tools are reached through /usr/local/bin symlinks; the server bin dir is dropped from PATH.
# TMPDIR points at writable scratch so tools work on a read-only root filesystem.
ENV HOME=/work \
    TMPDIR=/work \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /work
USER 65532:65532
STOPSIGNAL SIGTERM
ENTRYPOINT []
# Keepalive so the harness can exec into the running container.
CMD ["/usr/bin/sleep", "infinity"]
