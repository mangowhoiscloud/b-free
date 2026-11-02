## source-aws ↔ target-gcp

게이트 판정 **BLOCK**, 막힌 통제 ENC-01, NET-01. `collected_at` 2026-11-02T16:57:01.650475+00:00, `rule_version` gate-controls/2026-11-03.1, `input_digest` 305dee4a962e1c53…

| 통제 | 출발지 | 목적지 | 판정 | 이유 |
|---|---|---|---|---|
| SEC-01 | PASS (충족) | PASS (충족) | PASS |  |
| ENC-01 | PASS (충족) | FAIL (보완 필요) | BLOCK | regression |
| AUD-01 | PASS (충족) | PASS (충족) | PASS |  |
| NET-01 | FAIL (보완 필요) | PASS (충족) | BLOCK | not_equivalent |
| IAM-01 | PASS (충족) | PASS (충족) | PASS |  |
| DR-01 | PASS (충족) | PASS (충족) | PASS |  |

| 질문 | 상위 통제 | 출발지 | 목적지 | 판정 |
|---|---|---|---|---|
| OBJ-01 | IAM-01 | PASS `anonymous_read_denied` | PASS `anonymous_read_denied` | PASS |
| OBJ-02 | IAM-01 | PASS `anonymous_list_denied` | PASS `anonymous_list_denied` | PASS |
| OBJ-03 | IAM-01 | PASS `anonymous_write_denied` | PASS `anonymous_write_denied` | PASS |
| OBJ-04 | IAM-01 | PASS `app_read_write_allowed` | PASS `app_read_write_allowed` | PASS |
| OBJ-05 | IAM-01 | PASS `other_credential_denied` | PASS `other_credential_denied` | PASS |
| DB-ANON-READ | IAM-01 | PASS `public_cannot_select` | PASS `public_cannot_select` | PASS |
| DB-AUTH | IAM-01 | PASS `ok` | PASS `ok` | PASS |
| SEC-SECRET-READ-K8S | SEC-01 | PASS `ok` | PASS `ok` | PASS |
| SEC-SECRET-STORE-READ | SEC-01 | PASS `ok` | PASS `other_identity_denied` | PASS |
| OBJ-ENC | SEC-01 | PASS `customer_managed_key` | PASS `customer_managed_key` | PASS |
| SEC-SECRET-AT-REST | SEC-01 | PASS `ok` | PASS `ok` | PASS |
| OBJ-06 | ENC-01 | PASS `plain_http_denied_tls_only` | FAIL `plain_http_accepted` | BLOCK |
| DB-TLS | ENC-01 | PASS `ok` | PASS `ok` | PASS |
| DB-AUDIT | AUD-01 | PASS `ok` | PASS `ok` | PASS |
| AUD-CONTROL-PLANE | AUD-01 | PASS `ok` | PASS `admin_activity_recorded` | PASS |
| NET-DEFAULT-DENY | NET-01 | FAIL `default_deny_missing` | PASS `default_deny_present` | BLOCK |
| NET-PATHS | NET-01 | FAIL `denied_path_reachable` | PASS `only_allowed_paths_reachable` | BLOCK |
| DR-ARCHIVE | DR-01 | PASS `wal_archiving_healthy` | PASS `wal_archiving_healthy` | PASS |
| DR-RESTORE | DR-01 | PASS `restore_healthy` | PASS `restore_healthy` | PASS |

## source-aws ↔ target-onprem

게이트 판정 **BLOCK**, 막힌 통제 NET-01, IAM-01. `collected_at` 2026-11-02T16:56:47.149421+00:00, `rule_version` gate-controls/2026-11-03.1, `input_digest` f9ec96f074fd7820…

| 통제 | 출발지 | 목적지 | 판정 | 이유 |
|---|---|---|---|---|
| SEC-01 | PASS (충족) | PASS (충족) | PASS |  |
| ENC-01 | PASS (충족) | PASS (충족) | PASS |  |
| AUD-01 | PASS (충족) | PASS (충족) | PASS |  |
| NET-01 | FAIL (보완 필요) | PASS (충족) | BLOCK | not_equivalent |
| IAM-01 | PASS (충족) | FAIL (보완 필요) | BLOCK | regression |
| DR-01 | PASS (충족) | PASS (충족) | PASS |  |

| 질문 | 상위 통제 | 출발지 | 목적지 | 판정 |
|---|---|---|---|---|
| OBJ-01 | IAM-01 | PASS `anonymous_read_denied` | FAIL `anonymous_read_allowed` | BLOCK |
| OBJ-02 | IAM-01 | PASS `anonymous_list_denied` | PASS `anonymous_list_denied` | PASS |
| OBJ-03 | IAM-01 | PASS `anonymous_write_denied` | PASS `anonymous_write_denied` | PASS |
| OBJ-04 | IAM-01 | PASS `app_read_write_allowed` | PASS `app_read_write_allowed` | PASS |
| OBJ-05 | IAM-01 | PASS `other_credential_denied` | FAIL `other_credential_read_allowed` | BLOCK |
| DB-ANON-READ | IAM-01 | PASS `public_cannot_select` | PASS `public_cannot_select` | PASS |
| DB-AUTH | IAM-01 | PASS `ok` | PASS `ok` | PASS |
| SEC-SECRET-READ-K8S | SEC-01 | PASS `ok` | PASS `ok` | PASS |
| SEC-SECRET-STORE-READ | SEC-01 | PASS `ok` | PASS `other_identity_denied` | PASS |
| OBJ-ENC | SEC-01 | PASS `customer_managed_key` | PASS `customer_managed_key` | PASS |
| SEC-SECRET-AT-REST | SEC-01 | PASS `ok` | PASS `ok` | PASS |
| OBJ-06 | ENC-01 | PASS `plain_http_denied_tls_only` | PASS `plain_http_denied_tls_only` | PASS |
| DB-TLS | ENC-01 | PASS `ok` | PASS `ok` | PASS |
| DB-AUDIT | AUD-01 | PASS `ok` | PASS `ok` | PASS |
| AUD-CONTROL-PLANE | AUD-01 | PASS `ok` | PASS `apiserver_audit_recording` | PASS |
| NET-DEFAULT-DENY | NET-01 | FAIL `default_deny_missing` | PASS `default_deny_present` | BLOCK |
| NET-PATHS | NET-01 | FAIL `denied_path_reachable` | PASS `only_allowed_paths_reachable` | BLOCK |
| DR-ARCHIVE | DR-01 | PASS `wal_archiving_healthy` | PASS `wal_archiving_healthy` | PASS |
| DR-RESTORE | DR-01 | PASS `restore_healthy` | PASS `restore_healthy` | PASS |

