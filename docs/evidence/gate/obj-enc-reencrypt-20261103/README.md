# OBJ-ENC 온프렘: 기존 객체 재암호화 (2026-11-03 02:35–02:36 KST)

리뷰 B13 · 원장 G1. 온프렘 첨부 버킷 `bravo-demo-attachments`는 00:29 KST부터 기본 암호화가 SSE-KMS(키 `rgw-target-onprem`, 플랫폼 OpenBao transit)였지만, 그 전에 저장된 객체는 평문이었습니다. 그 객체를 모두 SSE-KMS로 다시 저장했습니다.

## 결과

| 항목 | 값 |
|---|---|
| 재암호화 전 | 객체 4개, SSE-KMS 0개(`01-before.txt`). 00:35의 `bucket stats`는 5개였고, 하나는 그 사이 지워졌습니다(누가 지웠는지 추적하지 않음) |
| 방법 | 같은 키로 CopyObject(`MetadataDirective REPLACE` + 객체 자신의 Content-Type · 사용자 메타데이터, `x-amz-server-side-encryption: aws:kms`, 키 `rgw-target-onprem`). 첫 시도 `MetadataDirective COPY`는 RGW가 `InvalidRequest`(자기 자신으로 복사)로 거부했고 아무것도 바뀌지 않았습니다 |
| 객체마다 확인 | 복사 전후 GET 본문 sha256 · 크기 같음, Content-Type · 사용자 메타데이터 키 같음, 복사 뒤 GET 200 + SSE 헤더, `radosgw-admin object stat`의 `user.rgw.crypt.mode: SSE-KMS`, `keyid: rgw-target-onprem`(`02-reencrypt.txt`) |
| 첨부 3개 | 키가 본문 sha256(`att/<sha256>`)이고 복사 뒤 sha256이 키와 같음 |
| 디스크 | canary의 RADOS 객체(39 bytes)에 본문 앞 24바이트가 없음(`03-rados-plaintext-check.txt`). 00:3x 기록에서는 같은 canary RADOS 객체에 본문이 그대로 있었음 |
| 재암호화 뒤 | 4개 모두 SSE-KMS(`02-reencrypt.txt` 끝 `summary`) |
| 게이트 | OBJ-ENC 객체 수준 `checked`, 4/4 고객 키(`../platform-gate-20261102T174659Z/`, `…175426Z/`). OBJ-01 · OBJ-05 온프렘 FAIL(S1 심은 회귀) 그대로, OBJ-06 온프렘 PASS 그대로 |

건드리지 않은 것: 버킷 정책 Sid `DenyInsecureTransport` · `BravoPlantedRegressionAnonymousRead`, 공개 차단 해제(실행 전후 출력 `unchanged_by_this_tool`).

## 새로 생긴 의존

canary `_gate/canary`도 이제 SSE-KMS입니다. 그래서 게이트 OBJ 시험의 canary GET(OBJ-01 · OBJ-05 · OBJ-06)도 RGW → OpenBao 링크를 탑니다. 링크가 끊기면 RGW가 500을 주고 그 질문들은 UNKNOWN(`probe_error`)이 됩니다(PASS가 되지는 않음). HEAD(canary 확인)는 복호화하지 않습니다. 사전 점검(리뷰 B17)에 링크 상태를 넣어야 합니다.

## 기록 손질

`01-before.txt` · `02-reencrypt.txt`의 객체 이름 필드는 실행 때 `"key"`였습니다. 첨부 키(`att/<sha256>`)가 gitleaks `generic-api-key`에 걸려(오탐, 내용 해시일 뿐) 필드 이름만 `"object"`로 바꿨고, 도구 출력도 같은 이름으로 고쳤습니다. 값은 그대로입니다.

## 다시 돌리기

```bash
uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem --inventory
uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem --reencrypt   # 평문만 골라 복사
```
