"""Dev-only Access-style JWT issuer for the pilot (CONTRACT.md 7).

Generates an RSA keypair (private key stays local and is gitignored), publishes the
public half as ``jwks.json`` and signs one token per (user, audience) pair in the same
shape Cloudflare Access emits: RS256, ``iss``, ``aud`` (list), ``exp``, ``iat``, ``sub``,
``email``. The platform verifies these with the same code path it will use for the real
Access issuer; only ``BRAVO_JWT_ISSUER`` and the JWKS source change.

Settings come from env, falling back to ``deploy/pilot/.env`` (found relative to this file):
``PLATFORM_JWT_ISSUER``, ``PLATFORM_CONSOLE_AUD``, ``PLATFORM_MCP_AUD``,
``PLATFORM_DEVAUTH_TOKEN_HOURS``, ``PLATFORM_DEVAUTH_OUT_DIR`` (default: this directory).

Usage:  uv run --project platform/api python deploy/pilot/devauth/gen.py [--rotate]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import jwt
from bravo_api.devseed import DEV_USERS, dev_sub  # same subjects as the DB seed
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = Path(__file__).resolve().parent
KID = "bravo-dev-1"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = HERE.parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and not key.lstrip().startswith("#"):
                values[key.strip()] = value.strip()
    values.update({k: v for k, v in os.environ.items() if k.startswith("PLATFORM_")})
    return values


def require(env: dict[str, str], key: str) -> str:
    if not env.get(key):
        raise SystemExit(f"{key} is not set (env or deploy/pilot/.env)")
    return env[key]


def load_or_create_key(path: Path, rotate: bool) -> rsa.RSAPrivateKey:
    if path.exists() and not rotate:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rotate", action="store_true", help="generate a new keypair")
    args = parser.parse_args()
    env = load_env()
    issuer = require(env, "PLATFORM_JWT_ISSUER")
    audiences = (require(env, "PLATFORM_CONSOLE_AUD"), require(env, "PLATFORM_MCP_AUD"))
    hours = float(env.get("PLATFORM_DEVAUTH_TOKEN_HOURS") or 12)
    out = Path(env.get("PLATFORM_DEVAUTH_OUT_DIR") or HERE)
    out.mkdir(parents=True, exist_ok=True)

    key = load_or_create_key(out / "private_key.pem", args.rotate)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    (out / "jwks.json").write_text(json.dumps({"keys": [jwk]}, indent=2) + "\n")

    tokens = out / "tokens"
    tokens.mkdir(exist_ok=True)
    now = int(time.time())
    for user, (email, _role) in DEV_USERS.items():
        for aud in audiences:
            claims = {
                "iss": issuer,
                "aud": [aud],
                "sub": dev_sub(email),
                "email": email,
                "iat": now,
                "nbf": now,
                "exp": now + int(hours * 3600),
                "type": "app",
            }
            token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})
            (tokens / f"{user}.{aud}.jwt").write_text(token + "\n")
    print(f"wrote {out / 'jwks.json'} and {len(DEV_USERS) * len(audiences)} tokens to {tokens}")
    for user, (email, role) in DEV_USERS.items():
        print(f"  {user:<9} role={role:<9} sub={dev_sub(email)}")


if __name__ == "__main__":
    main()
