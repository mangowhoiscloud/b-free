# PgBouncer from the PGDG apt repository on Debian bookworm (CONTRACT.md 4).
# Base image and package version are pinned; bump both deliberately.
ARG DEBIAN_IMAGE=debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
FROM ${DEBIAN_IMAGE}

ARG PGBOUNCER_VERSION=1.26.1-1.pgdg12+1
ARG PGB_UID=10070
ARG PGB_GID=10070
# PGDG signing key fingerprint (https://www.postgresql.org/download/linux/debian/).
ARG PGDG_KEY_FPR=B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    gpg --show-keys --with-colons /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      | grep -q "^fpr:::::::::${PGDG_KEY_FPR}:"; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends "pgbouncer=${PGBOUNCER_VERSION}"; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --system --gid "${PGB_GID}" bravo-pgb; \
    useradd --system --uid "${PGB_UID}" --gid "${PGB_GID}" --home-dir /nonexistent --no-create-home \
      --shell /usr/sbin/nologin bravo-pgb; \
    install -d -o bravo-pgb -g bravo-pgb -m 0775 /etc/pgbouncer; \
    rm -f /etc/pgbouncer/pgbouncer.ini /etc/pgbouncer/userlist.txt; \
    pgbouncer --version

USER bravo-pgb:bravo-pgb
EXPOSE 6432
# The config path is an env var so the image does not fix where the shared volume is mounted.
ENV PGBOUNCER_INI=/etc/pgbouncer/pgbouncer.ini
STOPSIGNAL SIGINT
ENTRYPOINT ["sh", "-c", "exec pgbouncer \"$PGBOUNCER_INI\""]
