# Bravo rehearsal service (05 4, 13): Harbor 0.25.0 Trials, the MCP tool layer and the server-side tools.
# Build context is platform/ (the bravo-agents path dependency lives in platform/agent):
#   docker build -f platform/rehearsal/Dockerfile platform
# Base image pinned by digest (python:3.12-slim, same as platform/gate).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION=0.10.2
# The venv lives at its final path (/app/.venv) so console-script shebangs stay valid in the runtime stage.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /app
COPY agent/pyproject.toml ./agent/pyproject.toml
COPY agent/src ./agent/src
COPY rehearsal/pyproject.toml rehearsal/uv.lock ./rehearsal/
WORKDIR /app/rehearsal
# Every package from uv.lock (hash-checked); bravo-agents installed from source, not editable.
RUN uv sync --frozen --no-dev --extra service --no-editable

FROM ${PYTHON_IMAGE}
ARG APP_UID=10001
RUN groupadd --system --gid "${APP_UID}" bravo \
 && useradd --system --uid "${APP_UID}" --gid "${APP_UID}" --home-dir /var/lib/bravo --no-create-home --shell /usr/sbin/nologin bravo \
 && mkdir -p /var/lib/bravo/runs && chown -R "${APP_UID}:${APP_UID}" /var/lib/bravo
WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root rehearsal/src ./src
COPY --chown=root:root rehearsal/tasks ./tasks
COPY --chown=root:root rehearsal/sql ./sql
COPY --chown=root:root agent/allowlists ./agent/allowlists
COPY --chown=root:root agent/harbor ./agent/harbor
ENV PATH="/app/.venv/bin:${PATH}" PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    BRAVO_AGENT_ALLOWLISTS_DIR=/app/agent/allowlists BRAVO_AGENT_HARBOR_DIR=/app/agent/harbor \
    BRAVO_REHEARSAL_TRIALS_DIR=/var/lib/bravo/runs HOME=/var/lib/bravo
USER bravo:bravo
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["uvicorn", "bravo_rehearsal.service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090"]
