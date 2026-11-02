## source-aws ↔ target-gcp

게이트 판정 **BLOCK**, 막힌 통제 SEC-01, ENC-01, AUD-01, NET-01, IAM-01. `collected_at` 2026-11-02T16:14:56.532517+00:00, `rule_version` gate-controls/2026-11-02.1, `input_digest` 8a277963d9772388…

| 통제 | 출발지 | 목적지 | 판정 | 이유 |
|---|---|---|---|---|
| SEC-01 | PASS (충족) | UNKNOWN (미검증) | BLOCK | unknown |
| ENC-01 | FAIL (보완 필요) | FAIL (보완 필요) | BLOCK | fail_both |
| AUD-01 | FAIL (보완 필요) | FAIL (보완 필요) | BLOCK | fail_both |
| NET-01 | FAIL (보완 필요) | UNKNOWN (미검증) | BLOCK | unknown |
| IAM-01 | UNKNOWN (미검증) | UNKNOWN (미검증) | BLOCK | unknown |
| DR-01 | PASS (충족) | PASS (충족) | PASS |  |

| 질문 | 상위 통제 | 출발지 | 목적지 | 판정 |
|---|---|---|---|---|
| OBJ-01 | IAM-01 | PASS `anonymous_read_denied` | PASS `anonymous_read_denied` | PASS |
| OBJ-02 | IAM-01 | PASS `anonymous_list_denied` | PASS `anonymous_list_denied` | PASS |
| OBJ-03 | IAM-01 | UNKNOWN `write_probe_disabled` | UNKNOWN `write_probe_disabled` | BLOCK |
| OBJ-04 | IAM-01 | UNKNOWN `app_credential_missing` | UNKNOWN `write_probe_disabled` | BLOCK |
| OBJ-05 | IAM-01 | PASS `other_credential_denied` | PASS `other_credential_denied` | PASS |
| DB-ANON-READ | IAM-01 | PASS `public_cannot_select` | PASS `public_cannot_select` | PASS |
| DB-AUTH | IAM-01 | PASS `ok` | PASS `ok` | PASS |
| SEC-SECRET-READ-K8S | SEC-01 | PASS `ok` | PASS `ok` | PASS |
| SEC-SECRET-READ-AWS | SEC-01 | PASS `ok` | UNKNOWN `not_configured` | BLOCK |
| OBJ-ENC | SEC-01 | PASS `customer_managed_key` | PASS `customer_managed_key` | PASS |
| SEC-ENC-CMK | SEC-01 | PASS `ok` | UNKNOWN `not_configured` | BLOCK |
| OBJ-06 | ENC-01 | PASS `plain_http_denied_tls_only` | FAIL `plain_http_accepted` | BLOCK |
| DB-TLS | ENC-01 | FAIL `plaintext_accepted` | FAIL `plaintext_accepted` | BLOCK |
| DB-AUDIT | AUD-01 | FAIL `log_connections_off` | FAIL `log_connections_off,pgaudit_log_incomplete,pgaudit_not_loaded` | BLOCK |
| AUD-PLATFORM | AUD-01 | PASS `ok` | UNKNOWN `not_configured` | BLOCK |
| NET-DEFAULT-DENY | NET-01 | FAIL `default_deny_missing` | PASS `default_deny_present` | BLOCK |
| NET-PATHS | NET-01 | UNKNOWN `not_configured` | UNKNOWN `not_configured` | BLOCK |
| DR-ARCHIVE | DR-01 | PASS `wal_archiving_healthy` | PASS `wal_archiving_healthy` | PASS |
| DR-RESTORE | DR-01 | PASS `restore_healthy` | PASS `restore_healthy` | PASS |

## source-aws ↔ target-onprem

게이트 판정 **BLOCK**, 막힌 통제 SEC-01, ENC-01, AUD-01, NET-01, IAM-01. `collected_at` 2026-11-02T16:14:44.502837+00:00, `rule_version` gate-controls/2026-11-02.1, `input_digest` 8d35a5cea0249234…

| 통제 | 출발지 | 목적지 | 판정 | 이유 |
|---|---|---|---|---|
| SEC-01 | PASS (충족) | UNKNOWN (미검증) | BLOCK | unknown |
| ENC-01 | FAIL (보완 필요) | FAIL (보완 필요) | BLOCK | fail_both |
| AUD-01 | FAIL (보완 필요) | FAIL (보완 필요) | BLOCK | fail_both |
| NET-01 | FAIL (보완 필요) | FAIL (보완 필요) | BLOCK | fail_both |
| IAM-01 | UNKNOWN (미검증) | FAIL (보완 필요) | BLOCK | unknown |
| DR-01 | PASS (충족) | PASS (충족) | PASS |  |

| 질문 | 상위 통제 | 출발지 | 목적지 | 판정 |
|---|---|---|---|---|
| OBJ-01 | IAM-01 | PASS `anonymous_read_denied` | FAIL `anonymous_read_allowed` | BLOCK |
| OBJ-02 | IAM-01 | PASS `anonymous_list_denied` | PASS `anonymous_list_denied` | PASS |
| OBJ-03 | IAM-01 | UNKNOWN `write_probe_disabled` | UNKNOWN `write_probe_disabled` | BLOCK |
| OBJ-04 | IAM-01 | UNKNOWN `app_credential_missing` | UNKNOWN `write_probe_disabled` | BLOCK |
| OBJ-05 | IAM-01 | PASS `other_credential_denied` | FAIL `other_credential_read_allowed` | BLOCK |
| DB-ANON-READ | IAM-01 | PASS `public_cannot_select` | PASS `public_cannot_select` | PASS |
| DB-AUTH | IAM-01 | PASS `ok` | PASS `ok` | PASS |
| SEC-SECRET-READ-K8S | SEC-01 | PASS `ok` | PASS `ok` | PASS |
| SEC-SECRET-READ-AWS | SEC-01 | PASS `ok` | UNKNOWN `not_configured` | BLOCK |
| OBJ-ENC | SEC-01 | PASS `customer_managed_key` | PASS `customer_managed_key` | PASS |
| SEC-ENC-CMK | SEC-01 | PASS `ok` | UNKNOWN `not_configured` | BLOCK |
| OBJ-06 | ENC-01 | PASS `plain_http_denied_tls_only` | PASS `plain_http_denied_tls_only` | PASS |
| DB-TLS | ENC-01 | FAIL `plaintext_accepted` | FAIL `plaintext_accepted` | BLOCK |
| DB-AUDIT | AUD-01 | FAIL `log_connections_off` | FAIL `log_connections_off,pgaudit_log_incomplete,pgaudit_not_loaded` | BLOCK |
| AUD-PLATFORM | AUD-01 | PASS `ok` | UNKNOWN `not_configured` | BLOCK |
| NET-DEFAULT-DENY | NET-01 | FAIL `default_deny_missing` | FAIL `default_deny_missing` | BLOCK |
| NET-PATHS | NET-01 | UNKNOWN `not_configured` | UNKNOWN `not_configured` | BLOCK |
| DR-ARCHIVE | DR-01 | PASS `wal_archiving_healthy` | PASS `wal_archiving_healthy` | PASS |
| DR-RESTORE | DR-01 | PASS `restore_healthy` | PASS `restore_healthy` | PASS |

