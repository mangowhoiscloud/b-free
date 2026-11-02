# Bravo gate: control tester and source-parity gate (FR-6, FR-12). Base image pinned by digest (python:3.12-slim).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
# SEC-GIT-PLAINTEXT (spec §8.5 "Git 평문 비밀 0건"): the official gitleaks image, static binary only, pinned by index digest.
ARG GITLEAKS_IMAGE=docker.io/zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f

FROM ${GITLEAKS_IMAGE} AS gitleaks

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION=0.10.2
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE}
ARG APP_UID=10001
RUN groupadd --system --gid "${APP_UID}" bravo \
 && useradd --system --uid "${APP_UID}" --gid "${APP_UID}" --home-dir /app --no-create-home --shell /usr/sbin/nologin bravo
WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --from=gitleaks --chown=root:root /usr/bin/gitleaks /usr/local/bin/gitleaks
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER bravo:bravo
EXPOSE 8084
ENTRYPOINT ["python", "-m", "bravo_gate"]
CMD ["serve"]
