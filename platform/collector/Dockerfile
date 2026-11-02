# Bravo collector (FR-1 read-only collection, FR-2 static inventory). Base image pinned by digest (python:3.12-slim).
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
COPY --chown=root:root sql /app/sql
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER bravo:bravo
EXPOSE 8083
ENTRYPOINT ["python", "-m", "bravo_collector"]
CMD ["serve"]
