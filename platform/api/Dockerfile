# Bravo platform API / operator MCP. One image, two modes (BRAVO_MODE=api|mcp).
# Base pinned by digest (§8.8). Build: docker build -t bravo-api:pilot platform/api
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir uv==0.10.2
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
RUN groupadd --gid 10001 bravo && useradd --uid 10001 --gid bravo --no-create-home --shell /usr/sbin/nologin bravo
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz',timeout=2)"]
CMD ["sh", "-c", "exec uvicorn bravo_api.app:create_app --factory --host 0.0.0.0 --port \"$PORT\" --no-server-header"]
