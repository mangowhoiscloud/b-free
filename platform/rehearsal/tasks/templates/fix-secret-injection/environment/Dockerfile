FROM python:3.12-slim
RUN useradd -m -u 10001 app
WORKDIR /app
COPY app/ /app/
USER 10001
