# apps/demo

이전 대상 데모 앱입니다. 앱 유형은 스펙 §8.10의 잠정안(금융권 사람 간 실시간 채팅, WebSocket)을 따르고, 기반 오픈소스 선택은 비워 둡니다. 데모 앱은 평가 대상이 아니므로 오픈소스를 써도 됩니다. T4(채팅 스텁)가 늦으면 T6, T16, T18도 늦어지므로 스텁 계약은 11/3에 고정합니다.

## 플랫폼이 앱에 요구하는 조건(§8.10)

어떤 앱을 고르든 아래를 지켜야 플랫폼이 동작합니다.

| 조건 | 이유 |
|---|---|
| PostgreSQL 16 이상, 모든 테이블에 PK, 앱 전용 로그인 역할 | 논리 복제와 펜싱의 전제 |
| 모든 쓰기에 클라이언트가 만든 멱등 키 | 쓰기 정지 중 재시도의 유실 · 중복 방지 |
| WebSocket이 끊기면 마지막 메시지 ID로 자동 재접속 | 컷오버 때 1012로 닫아도 사용자가 거의 못 느낌 |
| 첨부는 S3 SDK로 저장, 엔드포인트 설정 가능 | "설정 교체" 등급 의존성의 예 |
| 첨부 객체 키는 내용의 SHA-256(`att/<sha256>`). 덮어쓰지 않음. 교체는 새 행, 삭제는 DB의 `deleted_at`(제안, D-04) | 재전송이 중복을 만들지 않고, 차이분이 "새 키"뿐 |
| 첨부는 객체 저장 → SHA-256 확인 → DB 첨부 행 commit 순서. 행마다 논리 ID, 멱등 키(UNIQUE), 객체를 올린 저장소 `store_id`(제안, D-04) | DB와 객체 저장소를 한 트랜잭션으로 묶지 않아도 확정 참조가 어긋나지 않음 |
| 첨부 행 INSERT 트리거: `attachment_store.frozen`이거나 `store_id`가 `active_store`와 다르면 거부(제안, D-04) | 동결을 시간이 아니라 DB가 강제. 컷오버 뒤 옛 환경의 늦은 확정을 거부 |
| 첨부 행 UPDATE 트리거: key, sha256, `store_id` 변경 금지. `deleted_at`은 NULL에서 시각으로만(제안, D-04) | 확정 참조를 바꾸거나 삭제를 되돌리는 우회를 막음 |
| 앱 역할은 `attachment_store`와 트리거에 권한 없음. `session_replication_role`을 바꾸지 못함(제안, D-04) | 앱이 동결을 풀거나 트리거를 끄지 못함 |
| presigned URL, multipart 업로드, 앱 밖 직접 쓰기 없음. 첨부 크기 상한(예: 5 MB)(제안, D-04) | 우회 쓰기 경로를 없앰 |
| DB 비밀번호는 Secrets Manager SDK로 직접 조회 | "코드 수정" 등급. S2 데모의 재료(의도된 의존성) |
| QR 접속 페이지, 접속 환경(AWS, 온프렘) 표시 | 심사위원이 직접 참여하고 전환을 눈으로 확인 |
| 최대 트랜잭션 200ms 이하, DDL은 전환 창 밖 | PgBouncer PAUSE가 제때 끝나야 함 |
| 메시지 전파는 공통 쓰기 DB를 짧은 주기로 조회. 앱 메모리나 환경별 pub/sub에 기대지 않음 | 전환 중 두 환경의 앱이 같이 떠 있어도 서로의 메시지가 보임 |

## 플랫폼이 추가로 요청하는 것(제안)

| 요청 | 이유 |
|---|---|
| 쓰기 실패와 재시도 안내: 쓰기 정지 중 새 쓰기는 최대 8초 기다리고, 넘으면 재시도 안내(§5.3) | 사용자가 전환을 오류로 느끼지 않게 |
| 첨부가 동결이나 저장소 불일치로 거부되면 "첨부 일시 중지"로 안내하고, 활성 환경에서 다시 올린 뒤 같은 멱등 키로 재시도 | D-04의 동결 구간과 컷오버 직후 |
| 11/5까지 D-04가 준비되지 않으면 WATCH 동안 신규 첨부를 막고 "WATCH 신규 첨부 복구 미지원"을 보임 | D-04의 줄이는 범위. 기능 축소를 숨기지 않음 |
| 화면에 접속 환경과 `live` 표시 | 심사위원이 전환을 눈으로 확인 |

작업은 T4(v0), T22(v1)입니다.

## 구현(2026-11-02, T4 · T22 일부)

Python 3.12, FastAPI · uvicorn(WebSocket), psycopg 3, boto3. 기반 오픈소스 채팅은 쓰지 않고 최소 구현했습니다. 목업은 없습니다.

| 조건 | 이 앱이 지키는 방법 | 확인한 시험 |
|---|---|---|
| 모든 테이블 PK, 앱 전용 역할 | 새 표 `attachments`(uuid PK), `message_attachments`, `attachment_store`. `messages`의 열은 바꾸지 않음(Executor 해시 규칙 유지). 새 표는 시퀀스를 쓰지 않음(컷오버 때 Executor가 올리는 시퀀스는 `messages`, `bravo_barrier`뿐) | `test_schema.py::test_messages_columns_unchanged` |
| 멱등 키 | 메시지 `idem_key`, 첨부 `idem_key` 모두 UNIQUE. 같은 키 · 같은 내용이면 같은 행, 다른 내용이면 거부 | `test_send_is_idempotent`, `test_attachment_contract` |
| 1012 뒤 재접속 | 서버 종료(SIGTERM) 때 uvicorn이 1012로 닫음. 브라우저는 `after=<마지막 ID>`로 곧바로 다시 붙고, 응답을 못 받은 쓰기를 같은 키로 다시 보냄 | `test_fanout_across_instances_and_resume_after`, 실측: `docker stop` → 닫힘 코드 1012 |
| 쓰기 정지 중 8초 대기 | 쓰기마다 `WRITE_WAIT_S`(기본 8초) 안에서만 기다리고 재시도. 넘으면 화면에 "다시 보내기" 안내. 늦게 들어간 첫 시도가 있어도 같은 키라 한 번만 저장 | `test_write_waits_during_pause_then_retry_is_exactly_once`(실제 PgBouncer `PAUSE`) |
| 트랜잭션 200 ms 이하 | 쓰기 트랜잭션마다 `SET LOCAL statement_timeout`(≤ 200 ms). 세션 상태 없음, 서버 prepared statement 없음(PgBouncer transaction 모드) | 설정 검사 `test_config_statement_timeout_capped_at_200ms` |
| DB 조회로 전파 | 인스턴스마다 `messages`를 `id > 마지막`으로 0.5초마다 조회. 늦게 commit된 낮은 ID는 2초 동안 "빈 구간"으로 다시 읽음. LISTEN/NOTIFY · 메모리 pub/sub 없음 | `test_unit.py`(빈 구간, 시퀀스 점프), 두 인스턴스 시험 |
| 첨부 D-04 | `att/<sha256>` 키, `If-None-Match: *`로 덮어쓰지 않음 → 다시 읽어 SHA-256 확인 → 행 commit. 트리거가 동결(`ZB001`) · 저장소 불일치(`ZB002`)를 거부하면 HTTP 423 → 화면 "첨부 일시 중지". 같은 키로 다시 시도. presigned · multipart 없음, 5 MiB 상한(DB CHECK와 앱) | `test_schema.py` 12개(DB 강제). `test_attachment_*`는 RGW가 주어질 때만 |
| 비밀번호 | `DB_PASSWORD_SOURCE=aws-secrets-manager`이면 시작 때 Secrets Manager SDK 직접 호출(S2 재료). `env-file`이면 마운트한 파일(고친 뒤 모드) | Secrets Manager 시험은 실제 비밀이 있어야 하므로 건너뜀(`BRAVO_TEST_AWS_SECRET_ID`) |
| QR, 접속 환경, `live` | `/join`에 QR(`APP_PUBLIC_URL` 기준), 머리에 "연결된 곳: AWS/온프렘/GCP"와 `live` 배지. 환경이 바뀌면 안내 문구. 한국어 기본, 일본어 전환 | `test_pages_and_qr` |

### 실행

```bash
# 시험: 실제 PostgreSQL 16(src · dst, 논리 복제)과 파일럿 PgBouncer 이미지를 컨테이너로 띄움
uv sync && uv run pytest -q -rs
```

객체 저장소는 로컬 대용품(SeaweedFS, MinIO)을 쓰지 않습니다. 실제 저장소가 없으면 그 시험만 이유를 남기고 건너뜁니다.

| 시험 | 필요한 것 |
|---|---|
| 첨부 쓰기(`test_attachment_*`) | 온프렘 K3s의 Rook-Ceph RGW: `BRAVO_TEST_S3_ENDPOINT`, `BRAVO_TEST_S3_BUCKET`(이미 있는 버킷), 선택 `BRAVO_TEST_S3_REGION`, `BRAVO_TEST_S3_ADDRESSING_STYLE`, `BRAVO_TEST_S3_ACCESS_KEY_ID`, `BRAVO_TEST_S3_SECRET_ACCESS_KEY` |
| Amazon S3 읽기 전용(`test_amazon_s3_read_only`, HeadBucket · HeadObject만) | `BRAVO_TEST_AWS_S3_BUCKET`, `BRAVO_TEST_AWS_S3_REGION`(값은 `terraform -chdir=infra/envs/source-aws output`), 버킷 정책이 허용하는 AWS 자격 |
| Secrets Manager | `BRAVO_TEST_AWS_SECRET_ID`(선택 `BRAVO_TEST_AWS_SECRET_KEY`) |

```bash
# 파일럿과 함께. 버킷 값은 Terraform 출력에서 채움(읽기 전용)
apps/demo/scripts/render-pilot-env.sh          # apps/demo/pilot-app.env 생성
cd deploy/pilot
docker compose -p bravo-pilot --env-file .env --env-file ../../apps/demo/pilot-app.env \
  -f compose.yaml -f compose.app.yaml up -d --build app-src app-dst
# http://localhost:8090/join (AWS), http://localhost:8091/ (온프렘)
```

- 출발지 앱(`app-src`)의 첨부는 실제 Amazon S3입니다. 버킷과 리전은 `infra/envs/source-aws` 출력(`attachments_bucket`, `attachments_bucket_region`)에서 받습니다. 자격은 셸에서 넘긴 짧은 수명의 `AWS_*` 값(SDK 기본 체인)이고, 버킷 정책이 그 주체를 허용해야 씁니다.
- 목적지 앱(`app-dst`)의 첨부는 온프렘 K3s 랜딩 존의 Rook-Ceph RGW입니다. 엔드포인트와 버킷은 `infra/envs/target-onprem`이 생기면 그 출력에서 받습니다(제안한 출력 이름: `attachments_bucket`, `attachments_bucket_region`, `attachments_endpoint`). RGW 사용자 키는 랜딩 존 Secret에서 `pilot-app.env`에 넣습니다.
- 버킷 값이 비면 그 인스턴스는 첨부를 끄고(첨부 단추 숨김) 채팅만 합니다.
- 첨부를 쓰려면 채팅 DB가 이 스키마(2026-11-02 19:10 이후)로 만들어져 있어야 합니다(`scripts/reset_data_plane.sh`). 각 DB의 `attachment_store.active_store`는 기본으로 `src`, `dst`이고, 앱의 `ATTACHMENT_STORE_ID`와 같아야 합니다. 그래서 컷오버 전에는 AWS 앱만, 뒤에는 온프렘 앱만 첨부를 받습니다.

쿠버네티스(K3s · EKS · GKE)는 `chart/`를 그대로 씁니다. 다른 것은 값뿐입니다(`chart/ci/*-values.yaml`). 이미지는 digest로만 받습니다.

### 환경 변수

| 변수 | 뜻 |
|---|---|
| `APP_ENV_ID`, `APP_ENV_LABEL`, `APP_ENV_LABEL_JA` | 환경 ID(`aws`, `onprem`, `gcp`)와 화면 표시 |
| `APP_PUBLIC_URL` | QR이 가리킬 주소. 비우면 요청 주소 |
| `DB_DSN` | 비밀번호 없는 libpq DSN(PgBouncer). 비밀번호가 들어 있으면 시작을 거부 |
| `DB_PASSWORD_SOURCE` | `aws-secrets-manager` 또는 `env-file` |
| `DB_PASSWORD_SECRET_ID`, `DB_PASSWORD_SECRET_KEY`, `DB_PASSWORD_SECRET_REGION`(또는 `AWS_REGION`) | Secrets Manager 모드 |
| `DB_PASSWORD_FILE` | env-file 모드 |
| `WRITE_WAIT_S`(8), `DB_STATEMENT_TIMEOUT_MS`(200), `DB_POOL_MIN`, `DB_POOL_MAX` | 쓰기 대기, 트랜잭션 상한, 풀 |
| `POLL_INTERVAL_S`(0.5), `POLL_GAP_GRACE_S`(2) | 전파 조회 주기, 빈 구간 대기 |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ADDRESSING_STYLE`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY(_FILE)`, `S3_CONDITIONAL_PUT` | 모든 환경에서 같은 이름(Amazon S3, Rook-Ceph RGW, GCS XML API + HMAC). 키가 없으면 SDK 기본 체인(Pod Identity, IRSA, 셸의 `AWS_*`). `S3_BUCKET`이 비면 첨부를 끔 |
| `ATTACHMENT_STORE_ID`, `ATTACHMENT_MAX_BYTES`(≤ 5 MiB) | 첨부 저장소 ID, 크기 상한 |
| `AWS_REQUEST_CHECKSUM_CALCULATION`, `AWS_RESPONSE_CHECKSUM_VALIDATION` | 기본 `when_required`(S3 호환 저장소 · GCS 호환) |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | OTLP/gRPC. 지표 `chat.write.duration`, `chat.writes`, `chat.poll.duration`, `chat.ws.connections`, `chat.attachments` |

### 플랫폼 쪽에 남은 일(이 폴더 밖)

- Executor 컷오버 4단계에서 출발지 `attachment_store`를 동결해야 합니다(`UPDATE attachment_store SET frozen = true, freeze_watermark = now()`). `executor_ctl`에 권한은 줘 두었습니다. 동결 전 진행 중이던 INSERT는 트리거의 `FOR SHARE` 때문에 먼저 끝나므로 워터마크가 정확합니다.
- Executor 무결성 영수증(`DIGEST_TABLES`)은 아직 `attachments`, `message_attachments`를 비교하지 않습니다. 두 표는 uuid PK라 해시 규칙 v1(정수 PK)을 넓혀야 합니다.
- `executor_observe` 역할에는 새 표의 SELECT가 없습니다.
- 객체 복사(출발지 저장소 → 목적지 저장소, 새 키만)는 플랫폼 일입니다. 아직 복사되지 않은 첨부는 목적지 앱에서 404 `object_not_in_this_store`로 보입니다.
