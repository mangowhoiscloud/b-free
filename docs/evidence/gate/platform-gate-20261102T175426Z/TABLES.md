## source-aws ↔ target-gcp

게이트 판정 **BLOCK**, 막힌 통제 ENC-01. `collected_at` 2026-11-02T17:55:08.801879+00:00, `rule_version` gate-controls/2026-11-03.2, `input_digest` 92887add6cb01a78…

| 통제 | 출발지 | 목적지 | 판정 | 이유 |
|---|---|---|---|---|
| SEC-01 | PASS (충족) | PASS (충족) | PASS |  |
| ENC-01 | PASS (충족) | FAIL (보완 필요) | BLOCK | regression |
| AUD-01 | PASS (충족) | PASS (충족) | PASS |  |
| NET-01 | PASS (충족) | PASS (충족) | PASS |  |
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
| SEC-GIT-PLAINTEXT | SEC-01 | PASS `no_plaintext_secret` | PASS `no_plaintext_secret` | PASS |
| OBJ-06 | ENC-01 | PASS `plain_http_denied_tls_only` | FAIL `plain_http_accepted` | BLOCK |
| OBJ-PLAIN-EGRESS | ENC-01 | PASS `plain_http_blocked_app_https` | PASS `plain_http_blocked_app_https` | PASS |
| DB-TLS | ENC-01 | PASS `ok` | PASS `ok` | PASS |
| DB-AUDIT | AUD-01 | PASS `ok` | PASS `ok` | PASS |
| AUD-CONTROL-PLANE | AUD-01 | PASS `ok` | PASS `admin_activity_recorded` | PASS |
| NET-DEFAULT-DENY | NET-01 | PASS `default_deny_present` | PASS `default_deny_present` | PASS |
| NET-PATHS | NET-01 | PASS `only_allowed_paths_reachable` | PASS `only_allowed_paths_reachable` | PASS |
| NET-EXTERNAL-SCAN | NET-01 | PASS `only_published_entry_points` | PASS `only_published_entry_points` | PASS |
| DR-ARCHIVE | DR-01 | PASS `wal_archiving_healthy` | PASS `wal_archiving_healthy` | PASS |
| DR-RESTORE | DR-01 | PASS `restore_healthy` | PASS `restore_healthy` | PASS |

## source-aws ↔ target-onprem

게이트 판정 **BLOCK**, 막힌 통제 IAM-01. `collected_at` 2026-11-02T17:54:27.216159+00:00, `rule_version` gate-controls/2026-11-03.2, `input_digest` e20310e275b9742b…

| 통제 | 출발지 | 목적지 | 판정 | 이유 |
|---|---|---|---|---|
| SEC-01 | PASS (충족) | PASS (충족) | PASS |  |
| ENC-01 | PASS (충족) | PASS (충족) | PASS |  |
| AUD-01 | PASS (충족) | PASS (충족) | PASS |  |
| NET-01 | PASS (충족) | PASS (충족) | PASS |  |
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
| SEC-GIT-PLAINTEXT | SEC-01 | PASS `no_plaintext_secret` | PASS `no_plaintext_secret` | PASS |
| OBJ-06 | ENC-01 | PASS `plain_http_denied_tls_only` | PASS `plain_http_denied_tls_only` | PASS |
| OBJ-PLAIN-EGRESS | ENC-01 | PASS `plain_http_blocked_app_https` | PASS `plain_http_blocked_app_https` | PASS |
| DB-TLS | ENC-01 | PASS `ok` | PASS `ok` | PASS |
| DB-AUDIT | AUD-01 | PASS `ok` | PASS `ok` | PASS |
| AUD-CONTROL-PLANE | AUD-01 | PASS `ok` | PASS `apiserver_audit_recording` | PASS |
| NET-DEFAULT-DENY | NET-01 | PASS `default_deny_present` | PASS `default_deny_present` | PASS |
| NET-PATHS | NET-01 | PASS `only_allowed_paths_reachable` | PASS `only_allowed_paths_reachable` | PASS |
| NET-EXTERNAL-SCAN | NET-01 | PASS `only_published_entry_points` | PASS `only_published_entry_points` | PASS |
| DR-ARCHIVE | DR-01 | PASS `wal_archiving_healthy` | PASS `wal_archiving_healthy` | PASS |
| DR-RESTORE | DR-01 | PASS `restore_healthy` | PASS `restore_healthy` | PASS |

