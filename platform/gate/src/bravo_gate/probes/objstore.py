"""Object-store behaviour probes OBJ-01..OBJ-06 (D-22 research §4.4). Same S3 HTTP requests on every engine.

Only the endpoint and the credential source differ per environment (AWS S3, S3-compatible store, GCS XML API).
A fixed-key canary object created by the platform must exist: a 404 never counts as a pass, so a missing or
unverifiable canary makes every denial question (OBJ-01, 02, 03, 05, 06) UNKNOWN (``canary_missing`` /
``canary_unverified``).

What counts as "denied" (audit A5):
    PASS only on an explicit authorisation refusal: S3 error code AccessDenied / AllAccessDisabled, or a 403
    with no body (HEAD). InvalidAccessKeyId, SignatureDoesNotMatch, ExpiredToken and other errors mean the
    request was never evaluated against the policy, so they are UNKNOWN.
    OBJ-06 (plain HTTP) passes only on "connection refused" or an AccessDenied over http:// while the same
    credential reads the canary over https://. Timeouts, resets and DNS errors are UNKNOWN.
PUT probes (OBJ-03, OBJ-04 write half) run only when ``write_probes`` is true for that store AND the caller
allows writes (the CLI with ``--write-probes``; never an HTTP GET, audit 48). Every probe key the gate writes is
deleted again; the cleanup result is part of the evidence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

from ..environments import Credential, ObjectStoreSection
from ..result import Obs, error_text, failed, passed, unknown

ANON_PUT_KEY = "_gate/anon-put-probe"
APP_PUT_KEY = "_gate/app-put-probe"
APP_PUT_BODY = b"bravo gate app write probe\n"
DENIED = frozenset({"AccessDenied", "AllAccessDisabled"})
BAD_CREDENTIAL = frozenset({"InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "InvalidToken",
                            "TokenRefreshRequired", "AuthorizationHeaderMalformed"})
MISSING_KEY = frozenset({"NoSuchKey", "NotFound"})
MISSING_BUCKET = frozenset({"NoSuchBucket"})


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: str  # ok | denied | bad_credential | missing_key | missing_bucket | refused | timeout | error
    detail: str


def _classify(exc: BaseException) -> Outcome:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
        detail = f"{status} {code}".strip()
        if code in DENIED or (code in ("", "403") and status == "403"):
            return Outcome("denied", detail)  # body code AccessDenied, or HEAD 403 without a body
        if code in BAD_CREDENTIAL:
            return Outcome("bad_credential", detail)
        if code in MISSING_BUCKET:
            return Outcome("missing_bucket", detail)
        if code in MISSING_KEY or (code in ("", "404") and status == "404"):
            return Outcome("missing_key", detail)
        return Outcome("error", detail)
    if isinstance(exc, ConnectTimeoutError | ReadTimeoutError):
        return Outcome("timeout", type(exc).__name__)
    if isinstance(exc, EndpointConnectionError):
        inner = str(getattr(exc, "kwargs", {}).get("error", ""))  # urllib3 NewConnectionError text
        if "refused" in f"{exc} {inner}".lower():
            return Outcome("refused", "connection refused")
    return Outcome("error", error_text(exc))


def _attempt(fn) -> Outcome:
    try:
        fn()
        return Outcome("ok", "200")
    except Exception as exc:  # noqa: BLE001 - every failure is classified
        return _classify(exc)


def _endpoint(section: ObjectStoreSection, *, plain_http: bool = False) -> str:
    url = section.endpoint_url or f"https://s3.{section.region}.amazonaws.com"
    if plain_http:
        if section.plain_http_endpoint_url:  # the store's own plain listener behind its own link
            return section.plain_http_endpoint_url
        parts = urlsplit(url)
        url = urlunsplit(("http", parts.netloc, parts.path, parts.query, parts.fragment))
    return url


def _verify(section: ObjectStoreSection) -> str | bool:
    return section.ca_file or True


def _config(section: ObjectStoreSection, *, unsigned: bool) -> Config:
    kwargs: dict[str, Any] = {
        "connect_timeout": section.timeout_s,
        "read_timeout": section.timeout_s,
        "retries": {"max_attempts": 1, "mode": "standard"},
        "s3": {"addressing_style": section.addressing_style},
        "request_checksum_calculation": "when_required",
        "response_checksum_validation": "when_required",
    }
    if unsigned:
        kwargs["signature_version"] = UNSIGNED
    return Config(**kwargs)


def _session(cred: Credential, region: str) -> boto3.session.Session | None:
    if cred.profile:
        return boto3.session.Session(profile_name=cred.profile, region_name=region)
    if cred.access_key_env and cred.secret_key_env:
        ak, sk = os.environ.get(cred.access_key_env), os.environ.get(cred.secret_key_env)
        if not ak or not sk:
            return None
        return boto3.session.Session(aws_access_key_id=ak, aws_secret_access_key=sk, region_name=region)
    if cred.default_chain:
        return boto3.session.Session(region_name=region)
    return None


def _client(section: ObjectStoreSection, cred: Credential | None, *, plain_http: bool = False):
    if cred is None:
        return boto3.session.Session().client(
            "s3", region_name=section.region, endpoint_url=_endpoint(section, plain_http=plain_http),
            config=_config(section, unsigned=True), use_ssl=not plain_http, verify=_verify(section))
    sess = _session(cred, section.region)
    if sess is None:
        return None
    return sess.client("s3", region_name=section.region, endpoint_url=_endpoint(section, plain_http=plain_http),
                       config=_config(section, unsigned=False), use_ssl=not plain_http, verify=_verify(section))


class ObjectStoreProbe:
    """Runs the OBJ probes once per environment and caches the shared canary check."""

    def __init__(self, section: ObjectStoreSection, *, allow_writes: bool = False) -> None:
        self.s = section
        self.allow_writes = allow_writes
        self._canary: Outcome | None = None

    def _writes_off(self) -> str | None:
        if not self.s.write_probes:
            return "write_probe_disabled"
        if not self.allow_writes:
            return "write_probe_not_allowed_here"  # HTTP GET never writes; run the CLI with --write-probes
        return None

    def _cleanup(self, key: str) -> str:
        """Delete a probe key the gate wrote (app credential). Returns ok | <outcome> for the evidence."""
        app = _client(self.s, self.s.app_credential) if self.s.app_credential else None
        if app is None:
            return "no_app_credential"
        r = _attempt(lambda: app.delete_object(Bucket=self.s.bucket, Key=key))
        return "deleted" if r.kind == "ok" else f"{r.kind} {r.detail}"

    def _ev(self, **extra: Any) -> dict[str, Any]:
        return {"bucket": self.s.bucket, "endpoint": _endpoint(self.s), **extra}

    def canary(self) -> Outcome:
        """HEAD the canary with the app credential: ok = present, missing_key = absent."""
        if self._canary is None:
            cred = self.s.app_credential or self.s.read_credential
            app = _client(self.s, cred) if cred else None
            if app is None:
                self._canary = Outcome("unverified", "app_credential_missing")
            else:
                self._canary = _attempt(lambda: app.head_object(Bucket=self.s.bucket, Key=self.s.canary_key))
        return self._canary

    def _canary_gate(self) -> Obs | None:
        c = self.canary()
        if c.kind == "ok":
            return None
        if c.kind == "missing_key":
            return unknown("canary_missing", **self._ev(canary=self.s.canary_key, head=c.detail))
        return unknown("canary_unverified", **self._ev(canary=self.s.canary_key, head=f"{c.kind} {c.detail}"))

    def _denial(self, r: Outcome, ev: dict[str, Any], *, allowed: str, denied: str) -> Obs:
        """Common verdict for "must be refused" questions (A5, B17)."""
        if r.kind == "ok":
            return failed(allowed, **ev)
        gate = self._canary_gate()
        if gate is not None:
            return Obs(gate.verdict, gate.reason, {**gate.evidence, **ev})
        if r.kind == "denied":
            return passed(denied, **ev)
        if r.kind == "bad_credential":
            return unknown("credential_not_evaluated", **ev)
        if r.kind == "missing_bucket":
            return unknown("bucket_missing", **ev)
        return unknown("probe_error", **ev)

    # OBJ-01 unsigned GET of the canary must be denied.
    def obj01(self) -> Obs:
        anon = _client(self.s, None)
        r = _attempt(lambda: anon.get_object(Bucket=self.s.bucket, Key=self.s.canary_key)["Body"].read(0))
        ev = self._ev(request="unsigned GET", key=self.s.canary_key, response=r.detail)
        return self._denial(r, ev, allowed="anonymous_read_allowed", denied="anonymous_read_denied")

    # OBJ-02 unsigned LIST must be denied (canary present: the bucket is the real, non-empty one).
    def obj02(self) -> Obs:
        anon = _client(self.s, None)
        r = _attempt(lambda: anon.list_objects_v2(Bucket=self.s.bucket, MaxKeys=1))
        ev = self._ev(request="unsigned LIST", response=r.detail)
        return self._denial(r, ev, allowed="anonymous_list_allowed", denied="anonymous_list_denied")

    # OBJ-03 unsigned PUT must be denied (write probe).
    def obj03(self) -> Obs:
        off = self._writes_off()
        if off:
            return unknown(off, **self._ev(request="unsigned PUT", key=ANON_PUT_KEY))
        anon = _client(self.s, None)
        r = _attempt(lambda: anon.put_object(Bucket=self.s.bucket, Key=ANON_PUT_KEY, Body=b""))
        ev = self._ev(request="unsigned PUT", key=ANON_PUT_KEY, response=r.detail)
        if r.kind == "ok":
            return failed("anonymous_write_allowed", written_key=ANON_PUT_KEY, cleanup=self._cleanup(ANON_PUT_KEY),
                          **ev)
        return self._denial(r, ev, allowed="anonymous_write_allowed", denied="anonymous_write_denied")

    # OBJ-04 the app credential can read the canary and write a probe key (control path).
    def obj04(self) -> Obs:
        if self.s.app_credential is None:
            return unknown("app_credential_missing", **self._ev())
        app_head = _client(self.s, self.s.app_credential)
        c = (_attempt(lambda: app_head.head_object(Bucket=self.s.bucket, Key=self.s.canary_key))
             if app_head is not None else Outcome("unverified", "app_credential_unavailable"))
        ev = self._ev(request="app HEAD canary", key=self.s.canary_key, response=f"{c.kind} {c.detail}")
        if c.kind == "unverified":
            return unknown("app_credential_unavailable", **ev)
        if c.kind == "missing_key":
            return unknown("canary_missing", **ev)
        if c.kind == "denied":
            return failed("app_read_denied", **ev)
        if c.kind != "ok":
            return unknown("probe_error", **ev)
        off = self._writes_off()
        if off:
            return unknown(off, app_read="ok", **ev)
        app = _client(self.s, self.s.app_credential)
        w = _attempt(lambda: app.put_object(Bucket=self.s.bucket, Key=APP_PUT_KEY, Body=APP_PUT_BODY))
        ev["write"] = {"key": APP_PUT_KEY, "response": f"{w.kind} {w.detail}"}
        if w.kind == "ok":
            ev["write"]["cleanup"] = self._cleanup(APP_PUT_KEY)
            return passed("app_read_write_allowed", **ev)
        if w.kind == "denied":
            return failed("app_write_denied", **ev)
        return unknown("probe_error", **ev)

    # OBJ-05 a different credential must be denied reading the canary (a real AccessDenied only).
    def obj05(self) -> Obs:
        if self.s.other_credential is None:
            return unknown("other_credential_missing", **self._ev())
        other = _client(self.s, self.s.other_credential)
        if other is None:
            return unknown("other_credential_unavailable", **self._ev())
        r = _attempt(lambda: other.get_object(Bucket=self.s.bucket, Key=self.s.canary_key)["Body"].read(0))
        ev = self._ev(request="other-credential GET", key=self.s.canary_key, response=r.detail)
        return self._denial(r, ev, allowed="other_credential_read_allowed", denied="other_credential_denied")

    # OBJ-ENC default encryption at rest uses a customer-managed key (ENC-02). Read-only GetBucketEncryption.
    def obj_enc(self) -> Obs:
        cred = self.s.app_credential or self.s.read_credential
        if cred is None:
            return unknown("app_credential_missing", **self._ev())
        app = _client(self.s, cred)
        if app is None:
            return unknown("app_credential_unavailable", **self._ev())
        try:
            rules = app.get_bucket_encryption(Bucket=self.s.bucket)["ServerSideEncryptionConfiguration"]["Rules"]
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code == "ServerSideEncryptionConfigurationNotFoundError":
                return failed("no_default_encryption", **self._ev(code=code))
            return unknown("encryption_unreadable", **self._ev(code=code))
        except Exception as exc:  # noqa: BLE001
            return unknown("encryption_unreadable", **self._ev(error=error_text(exc)))
        algos = [(r.get("ApplyServerSideEncryptionByDefault") or {}) for r in rules]
        ev = self._ev(rules=[{"algorithm": a.get("SSEAlgorithm"), "kms_key": a.get("KMSMasterKeyID")} for a in algos])
        kms_keys = [a.get("KMSMasterKeyID") for a in algos if str(a.get("SSEAlgorithm", "")).startswith("aws:kms")]
        if not kms_keys:
            return failed("not_customer_key", **ev)
        if any(not k or "alias/aws/" in k for k in kms_keys):
            return failed("aws_managed_key", **ev)
        if self.s.endpoint_url is None:  # AWS: confirm KeyManager with KMS (read-only DescribeKey)
            sess = _session(cred, self.s.region)
            try:
                managers = sorted({sess.client("kms", region_name=self.s.region).describe_key(KeyId=k)[
                    "KeyMetadata"]["KeyManager"] for k in kms_keys}) if sess else []
            except Exception as exc:  # noqa: BLE001
                return unknown("kms_unreadable", error=error_text(exc), **ev)
            if managers != ["CUSTOMER"]:
                return failed("aws_managed_key", key_managers=managers, **ev)
        return passed("customer_managed_key", **ev)

    # OBJ-06 plain HTTP must be refused: connection refused, or AccessDenied over http:// while the same
    # credential reads the canary over https:// (so the refusal is about the transport, not the identity).
    # The credential is the app credential, else the read credential: the question is about the transport, and
    # any identity that may read the canary over https shows that the http refusal is not an identity refusal.
    def obj06(self) -> Obs:
        endpoint = _endpoint(self.s)
        if endpoint.startswith("http://"):
            return failed("endpoint_is_plain_http", **self._ev())
        cred = self.s.app_credential or self.s.read_credential
        if cred is None:
            return unknown("app_credential_missing", **self._ev())
        cred_kind = "app" if self.s.app_credential is not None else "read"
        https = _client(self.s, cred)
        h = (_attempt(lambda: https.head_object(Bucket=self.s.bucket, Key=self.s.canary_key))
             if https is not None else Outcome("unverified", "app_credential_unavailable"))
        if h.kind == "missing_key":
            return unknown("canary_missing", **self._ev(canary=self.s.canary_key, head=h.detail))
        if h.kind != "ok":
            return unknown("https_control_failed", **self._ev(head=f"{h.kind} {h.detail}"))
        plain = _client(self.s, cred, plain_http=True)
        r = _attempt(lambda: plain.get_object(Bucket=self.s.bucket, Key=self.s.canary_key)["Body"].read(0))
        ev = self._ev(request=f"{cred_kind} GET over http://", plain_endpoint=_endpoint(self.s, plain_http=True),
                      https_head="ok", response=f"{r.kind} {r.detail}")
        if r.kind == "ok":
            return failed("plain_http_accepted", **ev)
        if r.kind == "refused":
            return passed("plain_http_connection_refused", **ev)
        if r.kind == "denied":
            return passed("plain_http_denied_tls_only", **ev)
        return unknown("plain_http_inconclusive", **ev)  # timeout, reset, DNS, redirect, other errors
