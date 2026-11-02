# OBJ-ENC 온프렘: RGW SSE-KMS, 키는 플랫폼 OpenBao (2026-11-03 00:22–00:42 KST)

게이트 질문 OBJ-ENC의 목적지(`target-onprem`)가 FAIL `no_default_encryption`에서 **PASS `customer_managed_key`**로 바뀌었습니다. 첨부 버킷 `bravo-demo-attachments`의 기본 암호화는 `aws:kms`, 키 ID는 `rgw-target-onprem`입니다. 이 키는 플랫폼 OpenBao의 transit 엔진에만 있고 내보낼 수 없습니다(D-26). 온프렘 클러스터에는 키도 OpenBao도 없습니다.

## 구조

```
RGW (rook-ceph, Ceph 20.2.4)
  -> https://openbao:8200            Service rook-ceph/openbao = 링크 클라이언트 plat-bao-link (cloudflared access tcp)
  -> t1-plat-bao.mangowhois.ai       Cloudflare Access Service Auth (서비스 토큰 dst-plat-bao)
  -> 플랫폼 터널 bravo-platform -> openbao.openbao.svc:8200 (플랫폼 OpenBao)
```

- TLS는 OpenBao에서 끝납니다(링크 안에서 처음부터 끝까지). OpenBao 서버 인증서 SAN에 `openbao`가 있어서, 같은 네임스페이스의 Service 이름 `openbao`로 부르면 RGW의 호스트 이름 검사가 통과합니다(`rgw crypt vault verify ssl = true`). CA는 플랫폼 OpenBao CA(공개)입니다.
- 봉투 암호화입니다. PUT 때 RGW가 `transit/datakey/plaintext/rgw-target-onprem`을 불러 데이터 키와 그 키를 감싼 사본을 받습니다. 객체는 데이터 키로 암호화하고, 감싼 사본만 객체 속성 `user.rgw.crypt.datakey`에 둡니다. GET 때는 `transit/decrypt/rgw-target-onprem`으로 데이터 키를 풉니다.
- 매니페스트: `deploy/landing-zone/overlays/rke2/rook/objectstore.yaml`(`security.kms`), `deploy/landing-zone/overlays/rke2/rook/rgw-kms-link.yaml`(링크 클라이언트, Service, NetworkPolicy, 토큰 갱신 CronJob).

## 한 일

| 순서 | 시각(KST) | 명령 | 결과 | 파일 |
|---|---|---|---|---|
| 1 | 00:22:28 | `keys.sh rgw-kms-init target-onprem` | transit 키 `rgw-target-onprem`(aes256-gcm96, 내보내기 불가, 평문 백업 불가), 정책 · AppRole · 발급자 토큰 생성. root는 이 실행에서만 만들고 끝에 폐기 | `01-*` |
| 2 | 00:22:37 | `keys.sh rgw-kms-issue target-onprem` | RGW 토큰 발급(root 없이), OpenBao `secret/envs/target-onprem/rgw-kms`에만 저장 | `01-*` |
| 3 | 00:22:59 | 토큰 권한 실측(노트북, port-forward) | 허용 2개 200. 키 읽기 · encrypt · 다른 키 · sign · rotate · export · KV 읽기 · lookup-self · 토큰 생성은 모두 403. 컨텍스트를 붙인 datakey도 비유도 키에서 200 | `02-*` |
| 4 | 00:23:07 | `keys.sh issue-service-token target-onprem dst-plat-bao cf-link-plat-bao` | Access 서비스 토큰 `70361862-…`(168h) | `03-*` |
| 5 | 00:23–00:24 | 엣지 Terraform plan · apply(아래 절) | 변경 1건만 | `03-*` |
| 6 | 00:25:35 | `deploy/platform/scripts/onprem-tokens.sh rgw-kms` | Secret 세 개를 OpenBao에서 파이프로 만듦, 링크 적용, 새 토큰 발급, 이전 토큰 폐기 | `04-*` |
| 7 | 00:25:49 | 갱신 Job 수동 실행 | `renew-self HTTP 200, lease 3600s`(링크 · TLS 호스트 이름 · 토큰 확인). 라벨 없는 Pod는 링크에 못 닿음(`curl_exit=28`) | `04-*` |
| 8 | 00:27:56 | `rgw_encryption.py`(상태만) | 바꾸기 전: `404 ServerSideEncryptionConfigurationNotFoundError`. 심은 S1 Sid 있음 | `05-*` |
| 9 | 00:28:15 | `kubectl apply -f objectstore.yaml` | RGW 새 Pod(00:28:24 시작). 인자 `--rgw-crypt-s3-kms-backend=vault`, `--rgw-crypt-vault-addr=https://openbao:8200`, `--rgw-crypt-vault-prefix=/v1/transit`, verify ssl true. 토큰 파일 `ceph:ceph 400` | `06-*` |
| 10 | 00:29:24 | `rgw_encryption.py --apply` | `PutBucketEncryption` aws:kms + `rgw-target-onprem`. 정책 Sid · 공개 차단은 그대로 | `07-*` |
| 11 | 00:29–00:30 | `rgw_encryption.py --proof` | 아래 "증명" 절 | `08-*` |
| 12 | 00:31:17 | `rgw_regression.py --gate-report …`(상태만, 바꾸는 것 없음) | **OBJ-ENC src PASS · dst PASS → PASS**. OBJ-01 dst FAIL(심은 시나리오 그대로) | `10-*`, `gate-after-*.json` |
| 13 | 00:31:35 | `rgw_encryption.py --outage` | 링크를 0으로 줄였을 때 PUT · GET 500(아래 "새 의존" 절) | `11-*` |
| 14 | 00:34 | OpenBao 감사 기록 | datakey · decrypt · renew-self 기록 | `09-*` |
| 15 | 00:37:00 | CronJob `rgw-kms-renew` 첫 예약 실행 | `renew-self HTTP 200, lease 3600s` | `12-*` |
| 16 | 00:41:02 | `onprem-tokens.sh rgw-kms`(재발급 경로를 한 번 실제로 돌림) | 새 토큰, Secret, RGW Pod 재시작(00:41:09 KST 시작, 00:41:49 KST Ready, 약 40초 RGW 없음), 이전 토큰 폐기. 이어서 시험 객체 PUT · GET 200, 감사 기록의 접근자가 새 토큰. 심은 S1 Sid 그대로 | `12-*` |

## 증명

- **업로드.** SSE 헤더 없이 PUT(버킷 기본값 적용). 응답 200, `x-amz-server-side-encryption: aws:kms`, `x-amz-server-side-encryption-aws-kms-key-id: rgw-target-onprem`.
- **객체 속성**(`radosgw-admin object stat`). `user.rgw.crypt.mode: SSE-KMS`, `user.rgw.crypt.keyid: rgw-target-onprem`, `user.rgw.crypt.datakey: vault:v1:…`(89자, OpenBao가 감싼 데이터 키).
- **디스크.** RADOS 객체(65,619 bytes)에 평문 표지 문자열이 없습니다. 대조로 암호화 전에 저장된 canary의 RADOS 객체에는 본문 문자열이 그대로 있습니다.
- **다시 읽기.** GET 200, 같은 두 헤더, 65,619 bytes, sha256 같음. 시험 객체는 DELETE 204로 지움(`_obj-enc-proof/` 아래 남은 것 없음).
- **OpenBao 감사.** `transit/datakey/plaintext/rgw-target-onprem` · `transit/decrypt/rgw-target-onprem` 요청이 `10.42.0.218`(플랫폼 `cf-edge/cloudflared` Pod, 즉 링크를 거친 RGW)에서 왔고, 정책은 `platform-rgw-kms-target-onprem` 하나, 역할은 `rgw-kms-target-onprem`입니다. 감사 기록 안의 토큰 · 접근자 · 응답 값은 OpenBao가 HMAC으로 바꿔 둡니다.
- **게이트.** 게이트 CLI(플랫폼 게이트 이미지와 같은 코드, `ObjectStoreProbe.obj_enc`)의 목적지 증거: `{"verdict": "PASS", "reason": "customer_managed_key", "rules": [{"algorithm": "aws:kms", "kms_key": "rgw-target-onprem"}]}`.
- **건드리지 않은 것.** 버킷 정책 Sid `DenyInsecureTransport`, `BravoPlantedRegressionAnonymousRead`와 공개 차단 해제는 RGW 재시작 뒤에도 그대로입니다(서명 없는 canary GET 200, OBJ-01 dst FAIL). canary, 백업 버킷, chat-db는 건드리지 않았습니다.

## 토큰 수명과 갱신

| 항목 | 값 |
|---|---|
| 정책 `platform-rgw-kms-target-onprem` | `transit/datakey/plaintext/rgw-target-onprem`, `transit/decrypt/rgw-target-onprem`(update), `auth/token/renew-self`. 기본 정책 없음 |
| 토큰 | AppRole `rgw-kms-target-onprem`, service 토큰, TTL 1h, 갱신해서 최대 168h |
| 갱신 | CronJob `rook-ceph/rgw-kms-renew`(매시 7 · 22 · 37 · 52분)가 링크로 `renew-self`. Kubernetes API 권한 없음(SA 토큰 안 붙임). NetworkPolicy로 링크 8200과 DNS만 나감 |
| 발급 | `keys.sh rgw-kms-issue`. root가 필요 없음. 보관함의 발급자 토큰(정책 `platform-rgw-kms-issuer-target-onprem`)은 이 역할의 role-id 읽기와 일회용 secret-id(60초) 만들기만 함. 주기 168h, 쓸 때마다 갱신 |
| 재발급 | 168h 전에 `onprem-tokens.sh rgw-kms`: 새 토큰 -> Secret -> RGW Pod 재시작(RGW가 시작할 때 토큰 파일을 복사하기 때문) -> 이전 토큰 폐기(`keys.sh rgw-kms-retire`) |
| 지금 토큰 | 00:41 KST 재발급분(감사 HMAC 접근자 `40c83215`). 2026-11-09 15:41 UTC까지 갱신 가능. 앞선 두 토큰은 폐기됨. 링크 서비스 토큰 `dst-plat-bao`는 2026-11-09 15:23 UTC 만료. 둘 다 그 전에 다시 발급해야 함(서비스 토큰은 ID가 바뀌므로 Terraform도 다시) |

## 엣지 Terraform 변경

`infra/envs/edge/terraform.tfvars`(커밋 안 함)의 `platform_links.bao.token_ids`에 `70361862-e93c-46f4-962b-3a647bf04b1c`(dst-plat-bao)를 더했습니다. plan은 `cloudflare_zero_trust_access_policy.platform_link["bao"]` 한 건의 `include`에 서비스 토큰 하나를 더하는 변경뿐이었고, 그 plan 파일을 그대로 apply 했습니다(00:24 KST, `0 added, 1 changed, 0 destroyed`). 터널 ingress와 DNS는 이미 있었습니다. 설명은 `infra/envs/edge/platform_links.tf` 머리말에 적었습니다.

## 새 의존(실측)

- 이 버킷에 SSE-KMS로 저장한 객체는 PUT · GET마다 플랫폼 OpenBao를 부릅니다. 링크(또는 OpenBao, 토큰)가 없으면 읽지도 쓰지도 못합니다.
- 실측(00:31:37–00:31:39 KST, 링크 클라이언트를 0개로 줄임): PUT `500 InternalError`, 암호화 객체 GET `500 InternalError`. RGW 로그 `ERROR: Request to Vault failed with error -2200`, 요청마다 2 ms 안에 실패. 암호화 전에 저장된 canary GET은 200. 링크를 되살리자 같은 객체 GET 200, 바이트 같음. 끊긴 시간은 약 4초.
- 지연: SSE-KMS 객체 PUT · GET은 64 KiB에서 약 0.29초(RGW 로그 latency), 평문 객체 GET은 0.001초. RGW 재시작 뒤나 링크 재시작 뒤 첫 요청은 약 2.0초.
- 영향 범위: 데모 앱의 첨부 올리기 · 내려받기, Executor 첨부 복사(T8). T8은 객체마다 datakey 한 번이라 순차 복사면 객체당 약 0.29초가 더 듭니다. 게이트 OBJ 시험은 평문 canary만 읽으므로 영향이 없습니다.
- 갱신 Job이 1시간 넘게 못 돌거나 168h가 지나면 토큰이 죽고, 같은 500이 납니다. 갱신 Job 실패는 `kubectl -n rook-ceph get jobs -l app.kubernetes.io/name=rgw-kms-renew`로 봅니다.

## 남은 것 · 한계

- **기존 객체는 평문.** 기본 암호화를 켜기 전에 있던 객체 5개(canary 포함, `bucket stats` 00:35 KST)는 그대로입니다. RGW는 다시 암호화하지 않습니다. 이번 작업 범위는 암호화 설정뿐이라 옮기지 않았습니다.
- **고객 클러스터의 자격.** `rook-ceph`에 RGW 토큰 Secret과 링크 서비스 토큰 Secret이 있습니다. 키는 아니지만, `rook-ceph`의 Secret을 읽을 수 있는 주체는 둘을 합쳐 이 키로 데이터 키를 풀 수 있습니다. 토큰은 이 키 하나의 datakey · decrypt만 되고, 갱신이 멈추면 1시간 안에 죽습니다.
- **플랫폼 게이트는 아직 못 봄.** 플랫폼 등록부(`deploy/platform/control/config/environments.yaml`)의 `target-onprem`에 `object_store` 절이 없어 플랫폼 게이트(`bravo-control/gate`)에서는 OBJ 질문이 `not_configured` UNKNOWN입니다. 위 PASS는 같은 게이트 코드를 노트북 CLI로 돌린 결과입니다(S1 증거와 같은 한계, 게이트 갈래 몫).
- **RGW 재시작.** RGW가 한 대라 재시작 동안 S3 요청이 끊깁니다. 재발급 실행(00:41 KST)에서 새 Pod가 Ready가 되기까지 약 40초였습니다(Pod 시작 · Ready 시각 기준, 요청으로 따로 재지는 않음). 재발급은 데모 시간을 피해서 합니다.
- RGW 로그에 `cannot load crypto accelerator of type crypto_isal`이 한 번 나옵니다(arm64 이미지에 ISA-L 플러그인 없음). RGW는 기본 구현으로 암호화하고, 위 지연에 이미 들어 있습니다.

## 다시 돌리기

```bash
deploy/pilot/scripts/keys.sh rgw-kms-init target-onprem          # 한 번(root를 만들고 폐기)
deploy/pilot/scripts/keys.sh issue-service-token target-onprem dst-plat-bao cf-link-plat-bao
#   -> infra/envs/edge terraform.tfvars platform_links.bao.token_ids에 ID 추가, plan · apply
deploy/platform/scripts/onprem-tokens.sh rgw-kms                  # Secret, 링크, 토큰(168h 전에 다시)
kubectl --context bravo-onprem apply -f deploy/landing-zone/overlays/rke2/rook/objectstore.yaml
uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem --apply
uv run --project platform/gate python deploy/cloud/rgw_encryption.py --context bravo-onprem --proof
python3 deploy/cloud/rgw_regression.py --context bravo-onprem --gate-report <file>   # 상태만, 게이트 실행
bash deploy/landing-zone/tests/rgw-kms-renew-test.sh             # 갱신 스크립트 오프라인 시험
```

## 파일

| 파일 | 내용 |
|---|---|
| `01-openbao-rgw-kms-init.txt` | 키 · 정책 · AppRole · 발급자, 첫 토큰 |
| `02-token-scope.txt` | 토큰으로 되는 것과 안 되는 것(HTTP 코드만) |
| `03-edge-service-token.txt` | 서비스 토큰 발급, Terraform plan · apply |
| `04-onprem-tokens-rgw-kms-first.txt` | Secret · 링크 · 토큰 전달, 갱신 Job, NetworkPolicy 음성 확인 |
| `05-before.txt` | 바꾸기 전 버킷 상태 |
| `06-objectstore-kms-apply.txt` | CephObjectStore 적용, RGW 인자, 토큰 파일 권한 |
| `07-put-bucket-encryption.txt` | 기본 암호화 설정 |
| `08-proof-object.txt` | 시험 객체 네 번 실행. 첫 실행은 도구의 출력 디코딩 오류로 멈춤(객체는 지움). 두 · 세 번째는 RADOS 객체 이름(앞 `_` 이스케이프와 locator)을 잘못 잡아 디스크 확인이 빔. 네 번째가 최종. 끝에 canary 대조 |
| `09-openbao-audit.txt` | OpenBao 감사 기록 요약 |
| `10-gate-after.txt`, `gate-after-20261103T0031.json` | 게이트 실행 결과 |
| `11-outage.txt` | 링크 끊김 실측과 RGW 로그 |
| `12-reissue.txt` | 예약 갱신 실행, 재발급 경로(RGW 재시작 포함), 새 토큰으로 시험 객체 |
