# OBJ-06 GKE: 프로젝트 수준 조직 정책 시도 (2026-11-02 23:30 KST)

GKE 목적지의 첨부 버킷(GCS)은 평문 HTTP 요청을 받습니다. OBJ-06은 FAIL `plain_http_accepted`입니다. 이것을 막는 수단은 조직 정책 `constraints/storage.secureHttpTransport`입니다. 프로젝트 `b-free-target-20261102`에 직접 걸어 보았습니다. 걸리지 않았습니다.

## 측정

| 파일 | 내용 | 결과 |
|---|---|---|
| `01-policy.yaml` | 프로젝트 수준 정책 `enforce: true` | 걸려고 한 정책 |
| `02-set-policy.txt` | `gcloud org-policies set-policy` | 403 `IAM_PERMISSION_DENIED`, 권한 `orgpolicy.policies.create` |
| `03-iam.txt` | 프로젝트 상위와 역할 | 상위 없음(조직 · 폴더 없음). 운영자 계정은 `roles/owner`. 이 프로젝트에 줄 수 있는 조직 정책 역할은 `roles/orgpolicy.policyViewer` 하나뿐 |
| `04-effective.txt` | 적용 중인 정책 | `enforce: false` |
| `05-gate-obj06.json` | 플랫폼 게이트의 OBJ-06(같은 앱 HMAC으로 https HEAD 200, http GET 200) | 목적지 FAIL `plain_http_accepted` |

`orgpolicy.policies.create`는 `roles/orgpolicy.policyAdmin`에 있습니다. 이 역할은 조직 수준에서만 줄 수 있습니다. 조직이 없는 프로젝트는 소유자도 이 역할을 받을 수 없습니다. 그래서 프로젝트 수준 강제는 이 프로젝트에서 불가능합니다. `infra/envs/target-gcp`의 `enforce_storage_https`(기본 false)가 2026-11-02 apply에서 받은 403과 같은 원인입니다.

## 결론

- OBJ-06 GKE는 FAIL로 둡니다. PASS로 만들지 않습니다.
- 보완 통제는 그대로입니다. 노드에서 바깥 80 차단(방화벽 `bravo-target-gcp-egress-deny-80`), 앱 NetworkPolicy는 443만. 이것은 버킷이 평문을 받는 사실을 바꾸지 않습니다.
- 이 경우는 스펙 S1 5b "예외 승인"(대체 불가) 흐름의 대상입니다. 그 흐름은 다른 갈래가 만듭니다.
- 해소하려면 조직(Cloud Identity 또는 Workspace) 아래로 프로젝트를 옮겨 조직 정책을 걸어야 합니다. 팀장 결정 사항입니다.
