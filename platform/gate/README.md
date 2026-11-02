# platform/gate

통제 시험기와 원본 대조 게이트입니다(FR-6, FR-12, §8.5). 작업은 T5, T14, T19입니다. 설계는 `docs/architecture/04-governance.md` 1–3절에 있습니다.

같은 질문을 출발지와 목적지에 같은 방법으로 던지고, 결정적 규칙으로 판정합니다. 이 폴더는 LLM 클라이언트를 import하지 않습니다(NFR-5). `tests/test_rules.py`가 이를 검사합니다.

## 질문과 통제

질문 하나는 환경 설정의 한 절(`needs`)을 씁니다. 출발지나 목적지 중 한쪽이라도 그 절이 있으면 질문을 던집니다. 절이 없는 쪽은 `UNKNOWN`(`not_configured`)이고, 게이트를 막습니다.

| 통제 | 질문 ID | 시험(행동 시험이 우선) | 절 |
|---|---|---|---|
| IAM-01 · ISO-01 | OBJ-01 | 서명 없는 GET `<bucket>/_gate/canary` → 401 · 403이면 PASS | `object_store` |
| | OBJ-02 | 서명 없는 LIST → 401 · 403이면 PASS | `object_store` |
| | OBJ-03 | 서명 없는 PUT `_gate/anon-put-probe` → 거부면 PASS. `write_probes: true`일 때만 | `object_store` |
| | OBJ-04 | 앱 자격으로 canary HEAD, `_gate/app-put-probe` PUT → 둘 다 200이면 PASS(대조군) | `object_store` |
| | OBJ-05 | 다른 자격으로 canary GET → 403이면 PASS | `object_store` |
| | DB-ANON-READ | `has_table_privilege('public', 표, 'SELECT')`가 거짓이면 PASS | `postgres` |
| | DB-AUTH | 앱 역할로 비밀번호 없이 실제 로그인 → 거부, 그리고 `pg_hba`에 trust · peer · ident 규칙 없음, `scram-sha-256` | `postgres` |
| SEC-01 · ENC-02 | SEC-SECRET-READ-K8S | 다른 서비스 계정의 Secret `get` · `list`를 SubjectAccessReview로 시험 → 거부면 PASS | `kubernetes` |
| | SEC-SECRET-READ-AWS | 다른 IAM 주체의 `secretsmanager:GetSecretValue`를 `iam:SimulatePrincipalPolicy`(자원 정책 포함)로 시험 | `aws` |
| | OBJ-ENC | 첨부 버킷 기본 암호화가 고객 관리 키(`aws:kms`, AWS면 DescribeKey로 `KeyManager=CUSTOMER` 확인). `AES256`이나 설정 없음은 FAIL | `object_store` |
| | SEC-ENC-CMK | Secrets Manager 비밀의 `KmsKeyId`가 고객 관리 키(`KeyManager=CUSTOMER`) | `aws` |
| ENC-01 | OBJ-06 | 앱 자격으로 `http://` GET → 연결 거부 · 403이면 PASS. 엔드포인트 자체가 `http://`이면 FAIL | `object_store` |
| | DB-TLS | `sslmode=disable` 접속이 거부, `ssl=on`, 최소 TLS 1.2, CA로 `verify-full` 성공 | `postgres` |
| AUD-01 | DB-AUDIT | `pgaudit` 적재, `pgaudit.log`에 ddl · role(또는 all), `log_connections=on` | `postgres` |
| | AUD-PLATFORM | 다중 리전 CloudTrail이 기록 중, EKS면 `audit` 로그 켬 | `aws` |
| NET-01 | NET-DEFAULT-DENY | 앱 네임스페이스에 전체 Pod 대상 기본 거부(ingress, egress) NetworkPolicy | `kubernetes` |
| | NET-PATHS | 대조 경로는 TCP 연결 성공, 허용하지 않은 경로는 실패. 대조 경로가 실패하면 UNKNOWN | `network` |
| DR-01 | DR-ARCHIVE | `archive_mode` 켬, 한 번 이상 보관, 마지막 실패가 마지막 성공보다 이전 | `postgres` |
| | DR-RESTORE | `bootstrap.recovery`로 만든 CNPG Cluster가 healthy | `kubernetes` |

**canary.** OBJ-01, OBJ-05, OBJ-06은 플랫폼이 고정 키(`_gate/canary`)로 미리 올린 객체가 있어야 판정합니다. 앱 자격의 HEAD가 404이면 `canary_missing`, 앱 자격이 없으면 `canary_unverified`로 UNKNOWN입니다. 404가 PASS가 되는 일이 없습니다. AWS의 canary는 승인 전에는 만들지 않습니다.

**쓰기 시험.** OBJ-03과 OBJ-04의 PUT은 `write_probes: true`인 저장소에서만 돕니다. 기본값은 끔이고, 끄면 `write_probe_disabled`로 UNKNOWN입니다. 실제 AWS에는 켜지 않습니다.

**OBJ-06과 RGW.** 판정은 행동입니다. RGW에서 `aws:SecureTransport`를 `Bool`로 쓴 거부 정책은 평문 요청에 키가 없어 적용되지 않습니다. 그러면 평문 GET이 200을 받고 OBJ-06은 FAIL입니다. `BoolIfExists`로 써야 PASS가 됩니다.

**공개 차단 설정(OBJ-P).** 보조 근거입니다. 수집기 스냅숏(`platform/collector`)에 있고, 판정에는 쓰지 않습니다.

## 판정 규칙(04 §2)

| 원본 | 목적지 | 판정 | `reason` |
|---|---|---|---|
| PASS | PASS | PASS | - |
| PASS | FAIL | BLOCK | `regression` |
| FAIL | FAIL | BLOCK | `fail_both` |
| 어느 쪽이든 UNKNOWN | | BLOCK | `unknown` |
| FAIL | PASS | BLOCK | `not_equivalent` |

통제의 한쪽 값은 소속 질문을 모아 정합니다: FAIL이 하나라도 있으면 FAIL, 그다음 UNKNOWN, 모두 PASS면 PASS. 질문이 하나도 없는 통제는 UNKNOWN(`no_applicable_question`)입니다.

증거 문구는 네 상태로 씁니다.

| 값 | 문구 |
|---|---|
| PASS | 충족 |
| FAIL | 보완 필요 |
| 예외 승인 | 대체 불가 |
| UNKNOWN | 미검증 |

같은 입력이면 같은 출력입니다. 판정은 관찰 결과의 순수 함수이고, 관찰 결과로 `input_digest`(sha256)를 만듭니다. 시각 값(`collected_at`, `expires_at`)은 digest에 넣지 않습니다.

## kube-bench(근사)

`src/bravo_gate/kube_bench/mapping.yaml`이 kube-bench 항목을 통제 ID로 옮깁니다.

- kube-bench는 v0.16.0, 커밋 `5c6c22d5…`, 이미지 digest로 고정합니다.
- 벤치마크는 자동 감지하지 않고 `--benchmark`로 명시합니다: k3s `k3s-cis-1.9`, rke2 `rke2-cis-1.9`, eks `eks-1.8.0`, gke `gke-1.9.0`.
- 결과는 모두 "근사(approximate)"로 표시합니다. v0.16.0은 현재 판을 다 따라가지 못합니다(K3s 최신 대 CIS 1.9, CIS EKS 2.0.0 대 eks-1.8.0).
- 한쪽 플랫폼에만 있는 주제(예: EKS의 제어면 엔드포인트)는 다른 쪽을 미검증(UNKNOWN)으로 보입니다. 빼지 않습니다.
- `WARN`, `INFO`는 사람 확인이 필요하므로 UNKNOWN입니다.

`bravo_gate.kube_bench.normalize(report_json, platform)`과 `compare(src, dst)`가 이 표를 씁니다. 클러스터에서 kube-bench Job을 돌리는 일은 아직 하지 않았습니다.

## 환경 설정 파일

수집기와 같은 파일(`$BRAVO_ENVIRONMENTS_FILE`)을 읽습니다. 쓰지 않는 키는 무시합니다. 파일에는 참조만 둡니다. 비밀은 환경 변수와 표준 자격 체인에 둡니다.

```yaml
environments:
  src-aws:
    object_store:
      bucket: chat-attachments-t1
      region: ap-northeast-1             # endpoint_url 없음 = AWS S3
      app_credential: {default_chain: true}
      other_credential: {profile: rehearsal-test}
      write_probes: false
    aws:
      region: ap-northeast-1
      secret_ids: [chat/db]
      other_principal_arns: [arn:aws:iam::111122223333:role/other-app]
      eks_clusters: [source-eks]
    kubernetes: {kubeconfig_env: SRC_KUBECONFIG, app_namespace: chat, app_secret: chat-db,
                 other_service_accounts: [other/other-app], restore_cluster: restore-test/chat-restore}
    postgres: {dsn_env: SRC_PROBE_DSN, app_role: app_writer, app_tables: [messages],
               tls: {sslrootcert_env: SRC_PG_CA}}
    network: {control_paths: ["chat.src.internal:443"], denied_paths: ["10.0.3.10:5432"]}
  dst-onprem:
    object_store:
      bucket: chat-attachments-t1
      endpoint_url: https://rook-ceph-rgw-bravo.rook-ceph.svc:443
      addressing_style: path
      app_credential: {access_key_env: DST_S3_AK, secret_key_env: DST_S3_SK}
      other_credential: {access_key_env: DST_OTHER_AK, secret_key_env: DST_OTHER_SK}
    # ... 나머지 절은 출발지와 같은 모양
```

시험 DB 역할은 읽기 전용입니다. `platform/collector/sql/collector_ro.sql`의 `collector_ro`를 그대로 씁니다. DB-AUTH 시험이 앱 역할로 비밀번호 없이 접속해 보므로, 게이트 프로세스에 `PGPASSWORD`, `PGPASSFILE`이 있으면 그 시험은 UNKNOWN입니다.

## API

| 요청 | 응답 |
|---|---|
| `GET /v1/controls?source=ID&target=ID` | `CONTRACT.md` 6절 형식 그대로: `[{control_id, question, src, dst}]`, 값 `PASS` · `FAIL` · `UNKNOWN`. 질문마다 한 행 |
| `GET /v1/controls?source=ID&target=ID&level=control` | 같은 형식으로 통제마다 한 행(SEC-01, ENC-01, AUD-01, NET-01, IAM-01, DR-01) |
| `GET /v1/controls/report?source=ID&target=ID` | 같은 실행의 전체 결과: 행별 판정과 `reason`, 증거, 네 상태 문구, `input_digest`, `rule_version`, `test_version`, `collected_at`, `expires_at` |
| `GET /v1/catalog` | 통제와 질문 목록 |
| `GET /v1/environments` | 등록된 환경과 설정된 절(비밀 없음) |

플랫폼 API 게이트는 `BRAVO_EXECUTOR_URL`의 `/v1/controls`를 읽습니다. 이 서비스를 쓰려면 API 쪽에서 `source`와 `target`을 붙여 이 주소를 읽게 바꿔야 합니다. 이 폴더 밖이라 바꾸지 않았습니다.

## 실행

```bash
cd platform/gate
uv sync --frozen
BRAVO_ENVIRONMENTS_FILE=<registry.yaml> uv run --frozen python -m bravo_gate run --source <ID> --target <ID>
BRAVO_ENVIRONMENTS_FILE=<registry.yaml> uv run --frozen python -m bravo_gate run --source <ID> --target <ID> --report
BRAVO_ENVIRONMENTS_FILE=<registry.yaml> uv run --frozen python -m bravo_gate serve   # :8084 (BRAVO_GATE_PORT)
```

파일럿 네트워크에서: `deploy/pilot/compose.gate.yaml` 머리말을 보세요. 이미지는 uid 10001로 돌고, 루트 파일 시스템은 읽기 전용입니다.

## 시험

```bash
uv run --frozen pytest -q -rs
```

| 파일 | 대상 | 건너뛰는 조건 |
|---|---|---|
| `tests/test_rules.py` | 판정표, 모으기, 같은 입력 같은 출력, 계약 형식, 네 상태 문구, LLM import 금지 | 없음 |
| `tests/test_kube_bench.py` | 고정 판 · 명시 벤치마크, EKS 대 K3s 정규화, 한쪽에만 있는 주제 | 없음 |
| `tests/test_engines.py` | 실제 postgres:16 두 대(평문, TLS 강제 + CA), HTTP API, 실제 AWS S3 읽기 전용, 운영자가 지정한 실제 객체 저장소(Rook-Ceph RGW 또는 S3)의 OBJ 행동 시험 | docker가 없으면 DB 시험, AWS 자격이 없으면 AWS 시험, `BRAVO_TEST_S3_ENDPOINT` · `BRAVO_TEST_S3_BUCKET`이 없으면 객체 저장소 시험 |

객체 저장소 시험은 로컬 대체 엔진을 띄우지 않습니다(D-22: 온프렘 Rook-Ceph RGW, EKS S3, GKE Cloud Storage). 이미 있는 버킷을 환경 변수로 지정합니다.

| 변수 | 뜻 |
|---|---|
| `BRAVO_TEST_S3_ENDPOINT`, `BRAVO_TEST_S3_BUCKET` | 필수. 실제 RGW 또는 S3 엔드포인트와 이미 있는 버킷 |
| `BRAVO_TEST_S3_REGION`, `BRAVO_TEST_S3_ADDRESSING_STYLE` | 선택. 기본 `us-east-1`, `path` |
| `BRAVO_TEST_S3_ACCESS_KEY_ID`, `BRAVO_TEST_S3_SECRET_ACCESS_KEY` | 선택. 앱 자격. 없으면 SDK 기본 체인 |
| `BRAVO_TEST_S3_OTHER_ACCESS_KEY_ID`, `BRAVO_TEST_S3_OTHER_SECRET_ACCESS_KEY` | 선택. OBJ-05의 다른 자격 |
| `BRAVO_TEST_S3_CANARY_KEY` | 선택. 기본 `_gate/canary`. 플랫폼이 올린 canary |
| `BRAVO_TEST_S3_WRITE_PROBES=1` | 선택. 그 버킷에서 OBJ-03, OBJ-04의 PUT을 허용 |

canary가 없으면 OBJ-01, OBJ-05, OBJ-06이 PASS가 아님을 확인합니다. 시험은 canary나 버킷을 만들지 않습니다.

Kubernetes 질문(SEC-SECRET-READ-K8S, NET-DEFAULT-DENY, DR-RESTORE)은 아직 실제 클러스터에서 돌리지 않았습니다.
