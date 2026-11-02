"""Credential guard (NFR-6, D-26). The collector never returns or logs credential material.

Three layers use the same patterns:
    redact(text)            masks credential-shaped substrings in error strings (``model.failed``)
    install_log_filter()    masks them in every log record and keeps SDK debug logs off
    guard_json(value)       the HTTP middleware masks every string field of a JSON response; a field that is
                            still credential-shaped after masking is replaced by a "<withheld: …>" reason.
                            The rest of the response is returned (a test fixture never blocks a response).
"""

from __future__ import annotations

import logging
import re

# (name, pattern). Patterns match the credential value, not the surrounding text.
# A value that is already masked (``***``, ``<redacted:…>``, ``<withheld…>``) is not a credential.
_NOT_MASKED = r"(?!\*{3}|<redacted|<withheld)"

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*" + _NOT_MASKED + r"\S+")),
    ("aws_session_token", re.compile(r"(?i)aws_session_token\s*[=:]\s*" + _NOT_MASKED + r"\S+")),
    ("eks_bearer_token", re.compile(r"k8s-aws-v1\.[A-Za-z0-9_\-]{20,}")),
    ("sigv4_signature", re.compile(r"(?i)X-Amz-(?:Signature|Credential|Security-Token)=" + _NOT_MASKED
                                   + r"[^&\s\"']+")),
    ("bearer_header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{16,}=*")),
    ("password_kv", re.compile(r"(?i)\bpassword\s*=\s*" + _NOT_MASKED + r"[^\s'\"]+")),
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


# Stricter patterns for whole files (inventory scan and repository snapshot): real credential values only.
# Templated values (``{pw}``, ``$PW``, ``%s``, ``<pw>``, ``:'var'``) and AWS documentation keys do not count.
CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)(?![0-9A-Z]{9}EXAMPLE\b)[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", re.compile(r"(?i)aws_secret_access_key[\"']?\s*[=:]\s*[\"']?[A-Za-z0-9/+]{40}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("eks_bearer_token", re.compile(r"k8s-aws-v1\.[A-Za-z0-9_\-]{40,}")),
    ("sigv4_signature", re.compile(r"(?i)X-Amz-Signature=[0-9a-f]{64}")),
    # key = "v", key: "v" (YAML), "key": "v" (JSON). Not psql ``PASSWORD :'var'`` (space before the colon).
    ("password_literal", re.compile(r"(?i)(?:password|passwd|secret)(?:[\"']\s*:|\s*=|:)\s*[\"']"
                                    r"(?![{$%<*:])[^\"'\s]{8,}[\"']")),
    ("dsn_userinfo_password", re.compile(r"[a-z][a-z0-9+.-]*://[^/:@\s'\"]+:(?![{$%<*])[^@\s'\"/]{6,}@")),
)


def find_content_credentials(text: str) -> list[str]:
    """Names of credential patterns present in a file's text (values are never returned)."""
    return sorted({name for name, pat in CONTENT_PATTERNS if pat.search(text)})


def guard_json(value: object, path: str = "$") -> tuple[object, list[dict[str, object]]]:
    """Mask credential-shaped substrings in every string of a JSON value; withhold a field only if masking
    did not remove the shape. Returns (clean value, actions) where actions name the JSON path, never a value."""
    actions: list[dict[str, object]] = []

    def walk(v: object, p: str) -> object:
        if isinstance(v, str):
            hits = find_credentials(v)
            if not hits:
                return v
            masked = redact(v)
            left = find_credentials(masked)
            if left:
                actions.append({"path": p, "action": "withheld", "patterns": left})
                return f"<withheld: credential material ({', '.join(left)}) survived masking>"
            actions.append({"path": p, "action": "masked", "patterns": hits})
            return masked
        if isinstance(v, dict):
            return {k: walk(x, f"{p}.{k}") for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, f"{p}[{i}]") for i, x in enumerate(v)]
        return v

    return walk(value, path), actions
