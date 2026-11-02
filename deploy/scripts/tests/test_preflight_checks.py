"""Expiry rules of deploy/scripts/preflight_checks.py (the WARN/FAIL lines preflight.sh and rotate-credentials.sh act on).

    python3 -I -m unittest discover -s deploy/scripts/tests
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import unittest
from datetime import timedelta
from pathlib import Path

os.environ["PREFLIGHT_DEADLINE"] = "2026-11-09T00:00:00+09:00"
_spec = importlib.util.spec_from_file_location("preflight_checks", Path(__file__).resolve().parents[1] / "preflight_checks.py")
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


class ExpiryRules(unittest.TestCase):
    def test_openbao_nanosecond_timestamps_parse(self) -> None:
        dt = pc.parse_ts("2026-11-09T10:59:48.297967805Z")
        self.assertEqual((dt.year, dt.hour, dt.microsecond), (2026, 10, 297967))
        self.assertIsNone(pc.parse_ts(""))
        self.assertIsNone(pc.parse_ts("not a time"))

    def test_status_by_deadline_and_margin(self) -> None:
        self.assertEqual(pc.expiry(None), "PASS")                              # no expiry
        self.assertEqual(pc.expiry(pc.NOW - timedelta(minutes=1)), "FAIL")     # expired
        self.assertEqual(pc.expiry(pc.NOW + timedelta(minutes=90)), "FAIL")    # under 2 h
        before = pc.DEADLINE - timedelta(hours=1)
        if before - pc.NOW > pc.HARD_MARGIN:
            self.assertEqual(pc.expiry(before), "WARN")                        # ends before the demo deadline
        self.assertEqual(pc.expiry(pc.DEADLINE + timedelta(hours=1)), "PASS")

    def test_jwt_claims_reads_payload_only(self) -> None:
        payload = base64.urlsafe_b64encode(json.dumps({"exp": 1794000000, "sub": "system:serviceaccount:a:b"}).encode())
        tok = "eyJhbGciOiJSUzI1NiJ9." + payload.decode().rstrip("=") + ".c2ln"
        self.assertEqual(pc.jwt_claims(tok)["exp"], 1794000000)
        self.assertEqual(pc.jwt_claims("opaque-token"), {})
        self.assertEqual(len(pc.fingerprint(tok)), 8)


if __name__ == "__main__":
    unittest.main()
