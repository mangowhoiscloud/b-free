"""Client-side SCRAM-SHA-256 verifier for PostgreSQL (RFC 5802/7677, the format PostgreSQL stores).

``ALTER ROLE ... PASSWORD 'SCRAM-SHA-256$...'`` stores the verifier as given, so the plaintext never reaches
the server, its logs, or psql argv (audit B19). Usage:
    python -m bravo_collector scram-verifier < password-on-stdin  ->  prints the verifier
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import unicodedata

ITERATIONS = 4096  # PostgreSQL default scram_iterations


def scram_sha256_verifier(password: str, *, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    if not password:
        raise ValueError("empty password")
    salt = salt if salt is not None else os.urandom(16)
    # SASLprep-equivalent normalisation for ASCII/NFKC passwords (PostgreSQL falls back to raw bytes on failure).
    pw = unicodedata.normalize("NFKC", password).encode("utf-8")
    salted = hashlib.pbkdf2_hmac("sha256", pw, salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = base64.b64encode
    return (f"SCRAM-SHA-256${iterations}:{b64(salt).decode()}"
            f"${b64(stored_key).decode()}:{b64(server_key).decode()}")
