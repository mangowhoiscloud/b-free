# Optional relay image for registries that have no python3 image yet (contract §5: ECR, digest).
# The manifest mounts the same relay.py from a ConfigMap, so any python3 >= 3.9 image works too.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 link && useradd --system --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin link
COPY src/bravo_link/relay.py /opt/bravo-link/relay.py
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/opt/bravo-link/relay.py"]
