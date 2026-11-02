"""Database password sources (§8.10).

``aws-secrets-manager`` calls the AWS Secrets Manager API directly with the SDK at startup.
This direct cloud dependency is intentional: it is the "code fix" material of scenario S2.
``env-file`` reads a mounted file (Kubernetes Secret, Docker secret); this is the mode the
fix switches to, so the app no longer calls a cloud API.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import ConfigError, DbSettings

log = logging.getLogger(__name__)


class SecretError(RuntimeError):
    """The password could not be read. The message never contains the secret."""


def _from_file(path: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SecretError(f"cannot read DB_PASSWORD_FILE: {exc.strerror}") from exc
    if not value:
        raise SecretError("DB_PASSWORD_FILE is empty")
    return value


def _from_secrets_manager(settings: DbSettings) -> str:
    import boto3  # imported here: the env-file mode must not need AWS at all
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    if not settings.secret_id:
        raise ConfigError("DB_PASSWORD_SECRET_ID is required")
    client = boto3.client(
        "secretsmanager",
        region_name=settings.secrets_region,
        endpoint_url=settings.secrets_endpoint_url,
        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 3, "mode": "standard"}),
    )
    try:
        resp = client.get_secret_value(SecretId=settings.secret_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        raise SecretError(f"Secrets Manager GetSecretValue failed: {code}") from exc
    except BotoCoreError as exc:
        raise SecretError(f"Secrets Manager call failed: {type(exc).__name__}") from exc
    raw = resp.get("SecretString")
    if raw is None:
        raise SecretError("secret has no SecretString")
    if settings.secret_key:
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretError("secret is not JSON but DB_PASSWORD_SECRET_KEY is set") from exc
        value = doc.get(settings.secret_key) if isinstance(doc, dict) else None
        if not isinstance(value, str) or not value:
            raise SecretError("secret JSON has no string value for DB_PASSWORD_SECRET_KEY")
        return value
    if not raw:
        raise SecretError("secret string is empty")
    return raw


def db_password(settings: DbSettings) -> str:
    if settings.password_source == "env-file":  # noqa: S105 - a mode name, not a secret
        if not settings.password_file:
            raise ConfigError("DB_PASSWORD_FILE is required")
        value = _from_file(settings.password_file)
    elif settings.password_source == "aws-secrets-manager":  # noqa: S105 - a mode name
        value = _from_secrets_manager(settings)
    else:  # config.load() rejects anything else
        raise ConfigError("unknown DB_PASSWORD_SOURCE")
    log.info("database password loaded", extra={"source": settings.password_source})
    return value
