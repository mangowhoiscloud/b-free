"""Audit A5 / B17 rules that need no engine: S3 error classification, CloudTrail selectors, TCP outcomes.

S3 errors are real botocore ``ClientError`` objects with the response shapes S3 and RGW return.
TCP probes use real sockets (a closed local port, a reserved .invalid name).
"""

from __future__ import annotations

import socket

from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError

from bravo_gate.environments import NetworkSection
from bravo_gate.probes.objstore import _classify
from bravo_gate.probes.platform import _records_management_writes, network_paths


def _err(code: str, status: int) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
                       "GetObject")


def test_only_access_denied_counts_as_denied() -> None:
    assert _classify(_err("AccessDenied", 403)).kind == "denied"
    assert _classify(_err("403", 403)).kind == "denied"  # HEAD without a body
    for code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken"):
        assert _classify(_err(code, 403)).kind == "bad_credential", code
    assert _classify(_err("NoSuchKey", 404)).kind == "missing_key"
    assert _classify(_err("NoSuchBucket", 404)).kind == "missing_bucket"
    assert _classify(_err("PermanentRedirect", 301)).kind == "error"


def test_timeouts_are_not_refusals() -> None:
    assert _classify(ConnectTimeoutError(endpoint_url="http://x")).kind == "timeout"
    refused = EndpointConnectionError(endpoint_url="http://x", error="[Errno 61] Connection refused")
    assert _classify(refused).kind == "refused"
    other = EndpointConnectionError(endpoint_url="http://x", error="[Errno 8] nodename nor servname provided")
    assert _classify(other).kind == "error"


def test_cloudtrail_management_selectors() -> None:
    assert _records_management_writes({"EventSelectors": [{"IncludeManagementEvents": True,
                                                            "ReadWriteType": "All"}]})
    assert not _records_management_writes({"EventSelectors": [{"IncludeManagementEvents": False,
                                                                "ReadWriteType": "All"}]})
    assert not _records_management_writes({"EventSelectors": [{"IncludeManagementEvents": True,
                                                                "ReadWriteType": "ReadOnly"}]})
    adv = {"AdvancedEventSelectors": [{"FieldSelectors": [{"Field": "eventCategory", "Equals": ["Management"]}]}]}
    assert _records_management_writes(adv)
    adv_ro = {"AdvancedEventSelectors": [{"FieldSelectors": [
        {"Field": "eventCategory", "Equals": ["Management"]}, {"Field": "readOnly", "Equals": ["true"]}]}]}
    assert not _records_management_writes(adv_ro)


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_dns_error_is_not_counted_as_blocked() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    try:
        control = f"127.0.0.1:{srv.getsockname()[1]}"
        o = network_paths(NetworkSection(control_paths=(control,), denied_paths=("no-such-host.invalid:5432",)))
        assert o.verdict == "UNKNOWN" and o.reason == "denied_path_inconclusive", o
        o2 = network_paths(NetworkSection(control_paths=(control,), denied_paths=(f"127.0.0.1:{_closed_port()}",)))
        assert o2.verdict == "PASS", o2
    finally:
        srv.close()
