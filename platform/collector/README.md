# platform/collector

출발지와 목적지 설정을 읽기 전용으로 모읍니다(FR-1). 앱 저장소의 클라우드 의존성도 정적으로 찾습니다(FR-2). 작업은 새 작업 T30 제안(D-05)과 T16입니다. LLM을 import하지 않습니다(NFR-5).

출력은 T14(통제 시험기), T16(인벤토리), 상태 머신의 COLLECT 전이가 읽습니다. 형식은 아래 `collector/v1`입니다. 11/3 계약에서 고정합니다.

## 읽기 전용 보장

- AWS는 Describe, Get, List 호출만 합니다. `GetSecretValue`는 부르지 않습니다. 비밀은 메타데이터(KMS 키, 회전 설정, 자원 정책)만 읽습니다.
- Kubernetes는 get, list만 합니다. Secret은 `PartialObjectMetadataList`로 요청합니다. 그래서 API 서버가 값을 보내지 않습니다. 서버가 다른 형식으로 답하면 읽기를 거부합니다.
- PostgreSQL은 `collector_ro` 역할로 붙습니다. 세션도 `default_transaction_read_only=on`으로 엽니다. 이 역할에는 표 쓰기 권한이 없습니다. 비밀번호와 `archive_command` 본문은 읽지 않습니다(`archive_command`는 설정 여부만).
- 자격은 표준 경로에서만 받습니다. AWS는 기본 자격 체인(환경 변수, 공유 설정 · 프로필, SSO, web identity, IMDS), K8s는 kubeconfig, DB는 DSN을 담은 환경 변수입니다. 환경 파일에는 참조만 둡니다.
- 완료 기준 "수집 자격으로 쓰기가 거부된다"는 `tests/test_postgres.py::test_collector_credential_cannot_write`가 실제 PostgreSQL 16에서 확인합니다. AWS 쪽은 `tests/test_aws.py`가 `aws.py`에 쓰기 계열 호출이 없음을 검사합니다.

### 자격을 내보내지 않음

- 자격 값은 응답, 오류 문자열, 로그 어디에도 남기지 않습니다. `src/bravo_collector/redact.py`의 패턴(AWS 키 ID, 비밀 키, 세션 토큰, EKS 토큰 `k8s-aws-v1.`, SigV4 서명, Bearer 헤더, `password=`, 개인 키)을 세 곳에 씁니다.
  - 오류 문자열(`model.failed`)에서 가립니다.
  - 모든 로그 레코드에서 가리고, SDK의 통신 로그(botocore, urllib3, kubernetes)는 WARNING 위로만 둡니다.
  - HTTP 응답 본문에 그래도 남아 있으면 응답을 보내지 않고 500(`response withheld: credential material`)을 돌려줍니다.
- 시험은 `tests/test_redact.py`입니다.

### 임시 파일럿 경로: `~/.aws` 마운트

`deploy/pilot/compose.gate.yaml`은 운영자의 `~/.aws`를 수집기에만 읽기 전용으로 마운트합니다(EKS 실시간 보기용). 이것은 운영자의 장기 키를 플랫폼 컨테이너에 넣는 일이라 NFR-6과 D-26에 어긋납니다. 플랫폼이 EKS의 Pod Identity로 옮겨 가면 이 마운트를 지웁니다. 그때까지는 아래처럼 드러냅니다.

- compose가 `BRAVO_CREDENTIAL_SOURCE`에 "임시 파일럿 경로"를 적습니다.
- 수집기는 `GET /v1/environments`와 `GET /v1/clusters`의 `credential_source`(`label`, `temporary_pilot_path`)로 이 사실을 콘솔에 넘깁니다. 콘솔이 이 값을 표시합니다(콘솔 코드는 이 폴더 밖).
- 수집기가 쓰는 AWS 호출은 위 읽기 목록뿐입니다. 키의 실제 권한이 더 넓어도 수집기는 쓰기 호출을 하지 않습니다. 그래도 키 자체가 넓다는 위험은 남습니다.

### 필요한 읽기 권한

| 대상 | 권한 |
|---|---|
| AWS | `sts:GetCallerIdentity`, `s3:GetBucketPolicy`, `s3:GetBucketPolicyStatus`, `s3:GetBucketPublicAccessBlock`, `s3:GetEncryptionConfiguration`, `s3:GetBucketVersioning`, `s3:GetAccountPublicAccessBlock`, `iam:GetRole`, `iam:ListAttachedRolePolicies`, `iam:GetPolicy`, `iam:GetPolicyVersion`, `iam:ListRolePolicies`, `iam:GetRolePolicy`, `kms:DescribeKey`, `kms:GetKeyPolicy`, `kms:GetKeyRotationStatus`, `secretsmanager:DescribeSecret`, `secretsmanager:GetResourcePolicy`, `cloudtrail:DescribeTrails`, `cloudtrail:GetTrailStatus`, `cloudtrail:GetEventSelectors`, `eks:DescribeCluster`, `eks:ListPodIdentityAssociations`, `eks:DescribePodIdentityAssociation` (`aws.READ_ONLY_CALLS`) |
| K8s ClusterRole | `namespaces` get, list. `serviceaccounts`, `secrets`(메타데이터만 받음), `services` list. `apps/deployments` list. `networking.k8s.io/networkpolicies` list. `rbac.authorization.k8s.io/rolebindings` list |
| PostgreSQL | `collector_ro`. `sql/collector_ro.sql`이 만듭니다: `pg_read_all_settings`, `pg_read_all_stats`, `pg_monitor`, `pg_hba_file_rules` 읽기, DB CONNECT, 세션 기본 읽기 전용 |

`collector_ro` 만들기. 평문 비밀번호는 psql 인자에도, 서버로 가는 SQL에도 넣지 않습니다(감사 B19). 비밀번호는 표준 입력으로만 받아 클라이언트 쪽에서 SCRAM-SHA-256 검증값을 만들고, SQL 파일은 `\getenv`로 그 검증값만 받습니다. 검증값이 아니면 SQL이 멈춥니다.

    export COLLECTOR_RO_SCRAM_VERIFIER="$(<비밀번호를 내는 명령> | uv run --frozen python -m bravo_collector scram-verifier)"
    psql "$SUPERUSER_DSN" -v ON_ERROR_STOP=1 -f sql/collector_ro.sql

## 환경 파일

`BRAVO_ENVIRONMENTS_FILE`이 가리키는 YAML(또는 JSON) 파일입니다. 게이트(`platform/gate`)도 같은 파일을 읽고, 자기가 쓰지 않는 키는 무시합니다. 비밀 값은 넣지 않습니다.

```yaml
environments:
  src-aws:
    display_name: 출발지 AWS
    aws:
      region: ap-northeast-1
      profile: null                 # null이면 기본 자격 체인
      s3_buckets: [chat-attachments-t1]
      secret_ids: [chat/db-password]
      kms_key_ids: [alias/chat]
      iam_role_names: [chat-app]
      eks_clusters: [source-aws]
      app_namespaces: [chat]        # 이 네임스페이스의 Pod Identity 연결에서 앱 IAM 역할을 더 찾음
      cloudtrail_trails: []         # 비우면 보이는 trail 모두
    kubernetes:
      kubeconfig_env: SRC_KUBECONFIG  # kubeconfig 경로를 담은 환경 변수. 없으면 표준 위치
      context: null
      namespaces: [chat]            # 비우면 kube-system, kube-public, kube-node-lease 밖의 모두
    postgres:
      dsn_env: SRC_COLLECTOR_DSN    # libpq DSN을 담은 환경 변수(collector_ro)
      app_role: app_writer
      app_tables: [messages]
  bravo-source:
    display_name: 출발지 EKS
    kubernetes:                     # provider: eks + 아래 두 값이 있으면 kubeconfig 대신 boto3로 토큰을 만듦
      provider: eks                 # eks | k3s | null(일반 쿠버네티스)
      eks_cluster_name: bravo-source
      aws_region: ap-northeast-2
      context: bravo-source         # 표시용(호스트 kubeconfig 컨텍스트 이름)
  onprem-k3s:
    kubernetes: {provider: k3s, kubeconfig_env: BRAVO_ONPREM_KUBECONFIG}  # 변수가 없으면 not_configured
```

## 스냅숏 형식(`collector/v1`, D-05)

| 필드 | 뜻 |
|---|---|
| `schema_version` | `collector/v1` |
| `environment_id`, `collector_version` | 환경과 수집기 판 |
| `collected_at`, `expires_at` | 수집 시각과 만료 시각(UTC). 유효기간은 `BRAVO_EVIDENCE_TTL_S`(기본 3600초). 만료된 증거로는 승인하지 않습니다(§8.5) |
| `sources.{aws,kubernetes,postgres}` | `status`: `ok`, `partial`(어느 항목이 `error`), `error`(출처 전체 실패), `not_configured` |
| `sources.*.data` | 아래 항목의 트리 |

관측한 값은 모두 `{status, value, error}` 항목입니다.

| `status` | 뜻 | 읽는 쪽 |
|---|---|---|
| `ok` | 값을 읽음 | 값으로 판정 |
| `absent` | 자원이 "그 설정 없음"이라고 답함(예: `NoSuchBucketPolicy`, `NoSuchPublicAccessBlockConfiguration`) | "없음"으로 판정 |
| `error` | 읽지 못함(권한 거부, 시간 초과, 자원 없음) | UNKNOWN. 차단 |

출처별 내용:

- `aws`: `caller`, `account_public_access_block`, `s3_buckets.<이름>`(`policy`, `policy_status`, `public_access_block`, `encryption`, `versioning`), `eks_clusters.<이름>`(`cluster`: 엔드포인트 공개 · 비공개, 공개 CIDR, 로그, Secret 암호화 키, `pod_identity_associations`), `iam_roles.<이름>`(신뢰 정책, 붙은 정책 문서, 인라인 정책), `kms_keys.<id>`(`metadata`, `policy`, `rotation_enabled`), `secrets.<id>`(`metadata`: KMS 키, 회전, 마지막 회전 시각. `resource_policy`), `cloudtrail`(trail마다 `IsLogging`, 이벤트 선택기).
- `kubernetes`: `server_version`, `namespaces`(PSA 라벨), `by_namespace.<ns>`(`network_policies`와 계산한 `default_deny_ingress` · `default_deny_egress`, `service_accounts`(토큰 자동 마운트, IRSA 역할 주석만), `secrets`(이름, 생성 시각, 소유자 종류, `managed-by` 라벨만), `deployments`, `services`, `role_bindings`).
- `postgres`: `connection`(판, 사용자, `transaction_read_only`), `settings`(ssl, `ssl_min_protocol_version`, `password_encryption`, 접속 로그, `shared_preload_libraries`, `wal_level`, `archive_mode`, pgaudit), `archive_command.configured`, `own_ssl`, `hba_rules`(`options` 뺌), `extensions`, `roles`(비밀번호 없음), `app_tables.<표>`(`public_privileges`, `grants`: 소유자와 ACL), `app_role`, `archiver`.

## API(`:8083`)

| 요청 | 응답 |
|---|---|
| `GET /healthz` | `{status, version}` |
| `GET /v1/environments` | 환경 ID, 이름, 설정된 출처, `credential_source`(자격을 받는 방식의 표시, 값은 아님). 비밀 없음 |
| `GET /v1/environments/{id}/snapshot?sources=aws,kubernetes,postgres` | `collector/v1` 스냅숏. 모르는 환경은 404 |
| `GET /v1/clusters` | 콘솔용 클러스터 실시간 보기. `kubernetes` 절이 있는 환경마다 노드 · 네임스페이스 · 워크로드 · 파드 단계 수 · 랜딩 존 구성요소(`11-landing-zone.md` 2절 규칙표). get/list만, Secret은 읽지 않음. 호출마다 5초 제한, 실패한 클러스터는 `unreachable`, 설정이 없으면 `not_configured`. 4초 캐시 |
| `GET /v1/inventory` | `BRAVO_INVENTORY_ROOT`의 인벤토리(`inventory/v1`). 경로 인자는 받지 않습니다(경로 탈출 방지). 설정이 없거나, 읽을 파일이 하나도 없으면 503(`inventory_root_empty`: "의존성 없음"이 아니라 저장소가 없다는 뜻) |
| `GET /v1/inventory/snapshot` | 리허설용 저장소 스냅숏(S2, `15-r2-contracts.md`). 인벤토리가 읽은 파일만 담은 tar.gz, 헤더 `X-Bravo-Inventory-Digest`(인벤토리 `input_digest`와 같음), `X-Bravo-Snapshot-Files`, `X-Bravo-Snapshot-Skipped`. 같은 저장소면 같은 바이트(mtime 0, uid 0, 0644). 루트가 없거나 비면 503 |

## 비용(T1, 데모 마무리 장면)

    uv run --frozen python -m bravo_collector cost [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--tag component]

Cost Explorer를 읽기만 합니다. 요청마다 USD 0.01이 청구되므로 필요할 때만 돌립니다(한 번에 2–3건, 출력의 `cost_explorer_requests`).

- 먼저 `ce:ListCostAllocationTags`로 `component`가 비용 할당 태그로 켜져 있는지 봅니다. 켜져 있을 때만 태그별 비용을 냅니다.
- 켜져 있지 않으면 그 상태와 이유를 그대로 냅니다(`tag.status`, `tag.reason`). 켜는 것은 결제 설정이라 계정 소유자 승인 뒤에 사람이 합니다. 이 도구는 바꾸지 않습니다.
- 서비스별 비용은 늘 냅니다. 계정 전체 값이라 같은 계정의 다른 프로젝트도 들어 있습니다(`by_service.scope`). 지어낸 값은 없습니다.

## 실행

    uv sync --frozen
    BRAVO_ENVIRONMENTS_FILE=environments.yaml uv run --frozen python -m bravo_collector serve
    BRAVO_ENVIRONMENTS_FILE=environments.yaml uv run --frozen python -m bravo_collector snapshot --environment src-aws
    docker build -t bravo-collector:pilot .      # python:3.12-slim digest 고정, uid 10001로 실행

실제 대상(출발지 EKS, 온프렘 K3s)의 공용 등록 파일은 `deploy/environments/real-targets.yaml`입니다.

환경 변수: `BRAVO_ENVIRONMENTS_FILE`, `BRAVO_INVENTORY_ROOT`, `BRAVO_EVIDENCE_TTL_S`, `BRAVO_COLLECTOR_HOST`(기본 `0.0.0.0`), `BRAVO_COLLECTOR_PORT`(기본 8083), `LOG_LEVEL`. 그리고 환경 파일이 이름으로 가리키는 DSN · kubeconfig 변수입니다.

## 시험

    uv run --frozen pytest -q -rs

| 파일 | 대상 | 건너뛰는 조건 |
|---|---|---|
| `test_postgres.py`, `test_app.py` | 실제 `postgres:16.15` 컨테이너(digest 고정). `sql/collector_ro.sql`을 psql로 적용하고 시험 | docker가 없을 때 |
| `test_aws.py` | 실제 AWS 계정, 읽기 호출만(버킷 목록에서 최대 2개, CloudTrail) | 기본 자격 체인에 자격이 없거나 `sts:GetCallerIdentity`가 실패할 때 |
| `test_kubernetes.py` | `BRAVO_TEST_KUBECONFIG`(없으면 `~/.kube/config`)의 클러스터. Secret 메타데이터 목록 요청(kubernetes 클라이언트 37의 `param_serialize` · `call_api`)이 실제로 성공하는지 포함 | kubeconfig가 없거나 서버에 닿지 않을 때 |
| `test_redact.py` | 자격 패턴, 오류 · 로그 가림, 자격이 든 응답 차단, `credential_source` 표시 | 없음 |
| `test_inventory.py` | 임시 파일 트리와 실제 `apps/demo` | `apps/demo`가 없을 때 그 시험만 |

## 의존성 인벤토리(FR-2, T16)

앱 저장소를 정적으로 읽어 클라우드 의존성을 찾고, 규칙표로 3등급에 나눕니다. LLM을 부르지 않습니다(NFR-5). 규칙이 정하지 못한 항목만 `미분류`로 근거와 함께 남기고, 나중에 AI 제안 + 사람 판단으로 보냅니다(§8.1). 규칙표는 `src/bravo_collector/inventory/rules.py`의 `RULES`(데이터)이고, 판은 `RULES_VERSION`입니다.

| 등급 | 뜻 | 대표 규칙 |
|---|---|---|
| 설정 교체 | 환경마다 값만 바뀜. 코드는 그대로 | S3 클라이언트(엔드포인트가 설정 또는 `AWS_ENDPOINT_URL_S3`), 엔드포인트 · DSN 설정 키, EKS, CloudTrail, 리터럴 AWS 엔드포인트 · DSN |
| 어댑터 추가 | 번역 층이 필요 | IAM 역할 · 정책, IRSA 주석, 버킷 정책 · 공개 차단, Secrets Manager · KMS 자원과 ARN, ESO의 AWS 저장소 |
| 코드 수정 | 앱이 SDK로 직접 부름 | Secrets Manager 클라이언트와 `get_secret_value`, KMS 클라이언트와 `encrypt`/`decrypt`/`generate_data_key` |
| 이전 불필요 | §7.3 "그대로". 3등급과 따로 표시 | 환경 변수로 받는 PostgreSQL DSN, 변수로 조립한 DSN |
| 미분류 | 표 밖. AI 제안 대상 | 표에 없는 AWS 서비스 · Terraform 자원(RDS 포함), 판정할 수 없는 리터럴 URL 호스트 |

`hardcoded`는 리터럴(엔드포인트, DSN)이 설정 파일이 아니라 코드(Python, JS/TS, Go, Java, 셸, SQL)에 있을 때 `true`입니다. 코드 안의 DSN 리터럴은 환경마다 바꾸려면 코드를 고쳐야 하므로 `코드 수정` 등급으로 올립니다(감사 37). 증거 문자열은 비밀처럼 보이는 값(`DB_PASSWORD=`, `aws_secret_access_key =`, JSON `"password"`, URL userinfo, `password=`, AWS 키 ID, 개인 키)을 가립니다(감사 B18).

탐지: Python은 AST(클라이언트 생성, 변수에 묶인 클라이언트의 호출, 환경 변수 이름), 그 밖은 정규식(JS/TS, Go, Java, Terraform, YAML, JSON, `.env*`, Dockerfile)입니다. `.git`, `node_modules`, `.venv`, `dist`, `build`, `__pycache__`, 잠금 파일, 바이너리, 1 MiB 초과 파일, 심볼릭 링크, 문서(`.md`)는 읽지 않습니다. 주석 줄은 건너뜁니다.

**읽지 않는 파일(D-26).** 비밀 모양의 파일 이름(`.env`, `.env.*` 중 `.env.example` 같은 견본 제외, `*.pem`, `*.key`, `id_rsa`, `credentials`, `kubeconfig`, `*.tfstate`, `*.tfvars` 등)과, 내용에 자격 값이 있는 파일(`redact.CONTENT_PATTERNS`: AWS 키 ID · 비밀 키, 개인 키, EKS 토큰, SigV4 서명, 따옴표 안 비밀번호 리터럴, URL userinfo 비밀번호)은 읽지 않습니다. 그래서 `input_digest`와 스냅숏에도 없습니다. `skipped[]`에 이름과 이유만 남고, 자격 값이 있던 파일에는 `GEN-CREDENTIAL-FILE`(코드 수정) 항목이 첫 줄 번호와 함께 붙습니다. 값은 보고서에 들어가지 않습니다.

출력(`inventory/v1`): `schema_version`, `rules_version`, `root`(디렉터리 이름만), `input_digest`(읽은 파일의 상대 경로 + sha256), `files_scanned`, `files_skipped`, `skipped[]`(읽지 않은 후보 파일과 이유: 크기, 바이너리, UTF-8 아님, 읽기 실패), `summary`(등급별 의존성 수. 같은 줄 · 같은 등급 · 같은 서비스는 하나로 셈), `findings[]`, `unclassified[]`, `test_summary`, `test_findings[]`. 시험 코드(`tests/`, `test_*.py`, `conftest.py`, `*_test.go`, `*.spec.ts` 등)의 항목은 이전 대상이 아니므로 요약에서 빼고 `test_findings`에 따로 남깁니다. 버리지 않습니다. 항목 필드: `id`(규칙 · 파일 · 줄 · 일치 문자열의 sha256 앞 16자), `rule_id`, `tier`, `tier_code`, `service`, `kind`, `file`(루트 기준 상대 경로), `line`, `evidence`(그 줄, 비밀 값 가림, 최대 200자), `reason`, `hardcoded`, `confidence`(`rule`). 시각 값이 없어서 같은 입력이면 같은 출력입니다.

    BRAVO_INVENTORY_ROOT=../../apps/demo uv run --frozen python -m bravo_collector inventory
    uv run --frozen python -m bravo_collector inventory --path <앱 저장소> --label <이름>
    uv run --frozen python -m bravo_collector inventory --rules   # 규칙표 출력
