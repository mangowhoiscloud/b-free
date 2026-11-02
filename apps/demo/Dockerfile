# Bravo demo app (§8.10, T4/T22). Base image pinned by digest (python:3.12-slim, same as the Executor).
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

FROM ${PYTHON_IMAGE}
ARG APP_UID=10001
RUN groupadd --system --gid "${APP_UID}" bravo \
 && useradd --system --uid "${APP_UID}" --gid "${APP_UID}" --home-dir /app --no-create-home --shell /usr/sbin/nologin bravo
WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    APP_PORT=8000 \
    AWS_REQUEST_CHECKSUM_CALCULATION=when_required AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
# Numeric user so Kubernetes runAsNonRoot can verify it.
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/readyz' % os.environ.get('APP_PORT', '8000'), timeout=2).status == 200 else 1)"]
ENTRYPOINT ["python", "-m", "bravo_demo"]
