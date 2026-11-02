# 16. 스펙 §8.5 시험 → 게이트 질문 → 시험 방법

스펙 §8.5의 6개 통제 표는 통제마다 "이전 전후에 같은 방법으로 하는 시험"과 "도구"를 적습니다. 이 문서는 그 시험 하나하나가 게이트의 어느 질문으로 재지는지, 어떤 방법으로 재는지 적습니다. 게이트에 없는 시험은 [DEVIATIONS.md D-29](../spec/DEVIATIONS.md)에 이유와 함께 있습니다. 규칙 판은 `gate-controls/2026-11-03.2`(질문 22개)입니다. 리뷰 B12 · P16(2026-11-03)에 대한 답입니다.

## 1. 방법 세 가지

게이트 보고서의 질문 행마다 `method`가 있습니다. 콘솔은 이 값을 그대로 보여 줄 수 있습니다.

| 값 | 뜻 | 예 |
|---|---|---|
| `behavioural` | 실제 요청이나 연결을 보내고 그 답을 봅니다 | 서명 없는 GET → 403, 앱 라벨 Pod에서 TCP 연결 |
| `config` | 설정, 상태, 파일을 읽습니다 | GetBucketEncryption, NetworkPolicy 목록, gitleaks 파일 검사 |
| `simulation` | 정책 엔진에 "이 주체라면 어떻게 판단하나"를 묻습니다. 실제 접근은 하지 않습니다 | `iam:SimulatePrincipalPolicy`, SubjectAccessReview, `has_table_privilege` |

두 방법을 함께 쓰면 `behavioural+config`처럼 적습니다. 출발지와 목적지의 방법이 다르면 행의 `method`는 `mixed`이고, 각 쪽 방법은 `src_method` · `dst_method`, 도구는 `src_tool` · `dst_tool`에 있습니다. 행의 `spec_test`는 그 질문이 답하는 스펙 시험 문장입니다.

## 2. 대응표

"게이트 밖"은 지금 게이트가 재지 않는 시험입니다. D-29 항목 번호를 적었습니다.

| 통제 | 스펙 시험(§8.5) | 스펙 도구 | 게이트 질문 | 방법(출발지 EKS / 온프렘 RKE2 / GKE) | 실제 도구 |
|---|---|---|---|---|---|
| SEC-01 · ENC-02 | Git 평문 비밀 0건 | gitleaks | SEC-GIT-PLAINTEXT | config / config / config | gitleaks 8.30.1 `dir`, 배포 커밋의 고객 저장소(게이트 초기화 컨테이너가 받음). 이력은 아님(D-29 #8) |
| | 저장소 암호화 | - | OBJ-ENC | config / config / config | 버킷 기본 암호화 + 객체마다 HEAD(SSE 헤더). 출발지는 객체 목록 권한이 없어 버킷 수준만(증거 `object_level: not_checked_no_list_right`) |
| | | | SEC-SECRET-AT-REST | config / config / config | Secrets Manager · EKS 암호화 키(KMS) / OpenBao 상태 + kube-apiserver Secret 변환자 / OpenBao + GKE databaseEncryption |
| | 다른 서비스 계정의 Secret 읽기 거부 | kubectl 시험 | SEC-SECRET-READ-K8S | simulation(세 곳) | SubjectAccessReview(`kubectl auth can-i`와 같은 판단) |
| | | | SEC-SECRET-STORE-READ | **simulation** / behavioural / behavioural | EKS: `iam:SimulatePrincipalPolicy`. RKE2 · GKE: 권한 없는 OpenBao 역할로 실제 값 읽기 → 403 |
| | 회전 1회 | - | 게이트 밖 | - | D-29 #1 |
| | 로그 마스킹 | - | 게이트 밖 | - | D-29 #2 |
| ENC-01 | 앱–pooler–DB, 복제 구간마다 CA · hostname 검증 | psql verify-full | DB-TLS | behavioural+config(세 곳) | DB 구간: `sslmode=disable` 거부 + CA로 `verify-full` 성공 + `ssl`, 최소 TLS. pooler · 복제 구간은 게이트 밖(D-29 #4) |
| | TLS 다운그레이드 거부 | openssl | OBJ-06 | behavioural(세 곳) | 같은 자격으로 https 읽기 성공 뒤 `http://` GET. openssl 대신 boto3(D-29 #5) |
| | | | OBJ-PLAIN-EGRESS | behavioural+config(세 곳) | 앱 라벨 시험 Pod에서 저장소 TLS 포트 연결 성공 · 평문 포트 연결 실패 + 앱 Deployment의 `S3_ENDPOINT_URL`이 https. GKE OBJ-06 예외의 대체 통제 |
| AUD-01 | 정해 둔 이벤트(로그인, DDL, 권한 변경)를 실제로 검색 | pgaudit, RKE2 · CloudTrail 검색 | DB-AUDIT | config(세 곳) | pgaudit 적재 · `pgaudit.log` · `log_connections`. **검색은 아님**(D-29 #3) |
| | | | AUD-CONTROL-PLANE | config / config+behavioural / behavioural | EKS: CloudTrail 상태 · 선택자 · EKS audit 로그 설정. RKE2: 감사 플래그 + 시험 중 `apiserver_audit_event_total` 증가. GKE: Cloud Audit Logs 관리 활동 최근 1시간 검색 |
| NET-01 | 외부 포트 스캔에 공개 진입점만 보임 | nmap | NET-EXTERNAL-SCAN | behavioural(세 곳) | 플랫폼(고객 망 밖)에서 공개 주소마다 TCP connect 스캔, 포트 32개. `nmap -sT -Pn`과 같은 방식을 게이트 안에 구현(D-29 #5) |
| | 허용하지 않은 경로 거부 | NetworkPolicy 시험 | NET-PATHS | behavioural(세 곳) | 앱 라벨 · 이미지의 짧은 시험 Pod에서 대조 경로 연결, 금지 경로 실패 |
| | | | NET-DEFAULT-DENY | config(세 곳) | 앱 네임스페이스의 기본 차단 NetworkPolicy |
| IAM-01 · ISO-01 | 다른 앱 · 다른 역할의 자원 접근 거부 | 거부 시험 | OBJ-01 · 02 · 03 · 05 | behavioural(세 곳) | 서명 없는 GET · LIST · PUT, 다른 자격 GET |
| | | | OBJ-04(대조군) | **simulation** / behavioural / behavioural | EKS: 앱 역할의 `iam:SimulatePrincipalPolicy`(버킷 정책 · KMS 키 정책). 게이트는 앱의 IRSA 자격을 갖지 않음(D-29 #9). 목적지: 앱 키로 실제 HEAD · PUT · 삭제 |
| | | | DB-ANON-READ | simulation(세 곳) | `has_table_privilege('public', …)` |
| | | | DB-AUTH | behavioural+config(세 곳) | 앱 역할로 비밀번호 없이 실제 로그인 시도 + `pg_hba` 규칙 + `password_encryption` |
| | | kube-bench | 보고서 `cis_benchmark`(판정 안 함) | config | kube-bench v0.16.0 결과 요약을 증거로 붙임. 판정에 넣지 않음(D-29 #6) |
| | | Prowler | 게이트 밖 | - | D-29 #7 |
| DR-01 | 새 환경으로 실제 복원 성공 | CNPG 백업 · 복원 | DR-RESTORE | behavioural(세 곳) | `bootstrap.recovery`로 실제 복원한 클러스터(`chat-db-restore`)가 healthy |
| | | | DR-ARCHIVE | config(세 곳) | `pg_stat_archiver`(보관 횟수, 마지막 실패) |

## 3. 측정 결과 (2026-11-03 02:47–02:48 KST)

`docs/evidence/gate/platform-gate-20261102T174659Z/`(미리 보기 Pod, push 전). 새 질문 셋은 두 쌍 양쪽 모두 PASS입니다. 남은 FAIL은 의도한 것뿐입니다: 온프렘 OBJ-01 · OBJ-05(S1 심은 회귀), GKE OBJ-06(GCS 평문 HTTP, 예외 대상).

| 질문 | 출발지 EKS | 온프렘 | GKE |
|---|---|---|---|
| SEC-GIT-PLAINTEXT | PASS, 51파일, 발견 0 | 같음 | 같음 |
| OBJ-PLAIN-EGRESS | PASS: S3 443 연결, 80 시간 초과, 엔드포인트 미설정(SDK 기본 https) | PASS: RGW 443 연결, 80 시간 초과, `https://rook-ceph-rgw-bravo.rook-ceph.svc` | PASS: GCS 443 연결, 80 시간 초과, `https://storage.googleapis.com` |
| NET-EXTERNAL-SCAN | PASS: 엣지 80 · 443 · 8080 · 8443(Cloudflare), EKS API 엔드포인트는 플랫폼에서 모두 막힘 | PASS: 엣지만(원점 공개 주소 없음) | PASS: 엣지, GKE DNS 엔드포인트 80 · 443(Google 앞단), 꺼진 공개 IP 엔드포인트는 열린 포트 0 |
| OBJ-ENC 객체 수준 | 확인 안 함(목록 권한 없음) | 4개 모두 SSE-KMS | 1개 CMEK |

## 4. 보고서에 더한 키

| 키 | 자리 | 뜻 |
|---|---|---|
| `method`, `src_method`, `dst_method` | 질문 행 | 1절의 값 |
| `src_tool`, `dst_tool` | 질문 행 | 실제로 쓴 도구 |
| `spec_test` | 질문 행 | 답하는 스펙 시험 문장 |
| `cis_benchmark` | 보고서 | `{judged: false, label, src, dst}`. 쪽마다 벤치마크, 수집 시각, 만료(7일), 합계, FAIL 항목과 판단(`fixed` · `exception` · `not_applicable` · `unjudged`), 원본 경로. `status`는 `current` · `expired` · `not_run` |

증거 digest(`src_evidence_digest`, `dst_evidence_digest`)는 바뀌지 않은 규칙으로 계산합니다. 새 키는 증거 객체 밖에 있어 digest에 들어가지 않습니다.
