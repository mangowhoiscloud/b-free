"""Credential guard (NFR-6, D-26). The collector never returns or logs credential material.

Three layers use the same patterns:
    redact(text)            masks credential-shaped substrings in error strings (``model.failed``)
    install_log_filter()    masks them in every log record and keeps SDK debug logs off
    find_credentials(text)  the HTTP middleware withholds any response body that still contains one
"""

from __future__ import annotations

import logging
import re

# (name, pattern). Patterns match the credential value, not the surrounding text.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S+")),
    ("aws_session_token", re.compile(r"(?i)aws_session_token\s*[=:]\s*\S+")),
    ("eks_bearer_token", re.compile(r"k8s-aws-v1\.[A-Za-z0-9_\-]{20,}")),
    ("sigv4_signature", re.compile(r"(?i)X-Amz-(?:Signature|Credential|Security-Token)=[^&\s\"']+")),
    ("bearer_header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{16,}=*")),
    ("password_kv", re.compile(r"(?i)\bpassword\s*=\s*[^\s'\"]+")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def find_credentials(text: str) -> list[str]:
    return sorted({name for name, pat in PATTERNS if pat.search(text)})


def redact(text: str) -> str:
    for name, pat in PATTERNS:
        text = pat.sub(f"<redacted:{name}>", text)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not break logging
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        return True


_QUIET = ("botocore", "boto3", "urllib3", "kubernetes", "s3transfer")


def install_log_filter() -> None:
    """Mask credentials in every handler's records and keep SDK wire logging off (it can print headers)."""
    flt = _RedactFilter()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.addFilter(flt)
        for h in lg.handlers:
            h.addFilter(flt)
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
