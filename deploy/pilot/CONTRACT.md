# 파일럿 계약(2026-11-02 21:00 목표)

**내림(2026-11-02).** Compose 파일럿은 팀장 지시로 내렸습니다(`../CLOUD-E2E-CONTRACT.md` 6.1). 이 문서는 이력입니다. 수치는 "파일럿(노트북) 기록"(`docs/evidence/pilot-notebook/`)입니다.

스펙 P0의 한 줄기를 노트북에서 실제로 돌립니다: 운영자 MCP로 계획과 게이트 → 게이트 BLOCK → 고친 뒤 재대조 PASS → 콘솔 승인 → **실제 논리 복제 컷오버** → 쓰기 정지 ms와 무결성 영수증. 세 갈래(데이터면 · Executor, API · MCP, 콘솔)가 이 문서만 보고 따로 만듭니다. 바꿀 때는 이 문서를 먼저 고칩니다.

- 실행은 Docker Compose(`deploy/pilot/compose.yaml`)입니다. 목표 실행 환경(EKS · K3s · GKE + Helm + CNPG)은 그대로이고, 이미지와 설정(환경 변수)은 그대로 옮겨 갑니다.
- 목업은 어디에도 없습니다. 리허설(S2, ANALYZE · PROPOSE · REHEARSE)은 2026-11-02부터 실제 경로가 있습니다(`platform/api/README.md` "상태 경로와 리허설"). 모델 키나 목적지 클러스터가 없으면 `llm_unconfigured` · `cluster_unconfigured`로 멈추고 그대로 보입니다. 게이트 BLOCK은 사람이 `fix-dst-grant.sql`로 고치거나 리허설로 재제안합니다.
- 경로 · 주소 · 비밀은 모두 환경 변수(`deploy/pilot/.env.example`)로 받습니다. 스크립트는 자기 위치 기준 상대 경로만 씁니다. 이미지는 모두 digest로 고정합니다.

## 1. 스택(파일럿과 본선 공통)

| 층 | 고른 것 |
|---|---|
| 서비스 언어 | Python 3.12, uv 작업 공간. 타입 검사 `pyright`(basic), 린트 `ruff` |
| API · MCP | FastAPI, Pydantic v2, uvicorn. MCP는 공식 `mcp` 2.3.0(`stateless_http=True`, 경로 `/mcp`) |
| DB 접근 | psycopg 3(`psycopg[binary,pool]`). ORM 없음. SQL 마이그레이션 파일 + 적용 기록 표 |
| 인증 | Cloudflare Access JWT와 같은 형식(RS256, `iss`, `aud`, `exp`, `iat`, `sub`, `email`)을 PyJWT로 검증. 파일럿은 로컬 발급자(`deploy/pilot/devauth/`)가 같은 형식으로 서명 |
| 데이터면 | PostgreSQL 16(출발지, 목적지, 플랫폼 DB 각 1), 논리 복제, PgBouncer(transaction 모드) |
| 콘솔 | TypeScript, React 19, Vite, TanStack Query. 진행은 SSE(헤더를 실어야 하므로 `fetch` 스트리밍). 개발용 토큰은 `GET /v1/dev/tokens`(`BRAVO_DEV_AUTH=1`일 때만) |
| 관측 | OpenTelemetry SDK → OTLP → OpenTelemetry Collector → Prometheus(지표, OTLP 수신) · Tempo(트레이스) · Loki(로그) → Grafana. 개발용 올인원 이미지는 쓰지 않음 |
| 시험 | pytest. 서비스마다 단위 시험 + 파일럿 통합 시나리오 스크립트 |

## 2. Compose 서비스와 주소

| 서비스 | 이미지 · 위치 | 포트(호스트) | 하는 일 |
|---|---|---|---|
| `src-db` | PostgreSQL 16 공식 이미지(`CHAT_DB_IMAGE`, digest 고정). CNPG 운영자 없음 | 15432 | 출발지 채팅 DB. `wal_level=logical` |
| `dst-db` | 같은 이미지 | 25432 | 목적지 채팅 DB. `wal_level=logical` |
| `pgbouncer` | PGDG 패키지로 직접 빌드(`deploy/pilot/pgbouncer/Dockerfile`) | 16432 | 앱 쓰기 경로. 처음 대상은 `src-db` |
| `platform-db` | PostgreSQL 16 공식 이미지(`postgres:16.13`, digest 고정). CNPG 운영자 없음 | 35432 | 플랫폼 DB(계획, 이벤트, 승인, 실행, 효과, 영수증) |
| `executor` | `platform/executor/` | 8081 | 컷오버 실행(§8.6). 플랫폼 DB에 직접 기록 |
| `api` | `platform/api/` | 8080 | REST `/v1/*`, SSE. 콘솔이 부름 |
| `operator-mcp` | `platform/api/` 같은 이미지, `BRAVO_MODE=mcp` | 8082 | 운영자 MCP 서버 `/mcp` |
| `loadgen` | `tools/loadgen/` | - | PgBouncer로 채팅 쓰기. 요청 ID와 시도 ID 기록 |
| `console` | `platform/console/` | 5173 | 콘솔 |
| `otel-collector` | `otel/opentelemetry-collector-contrib` 0.164.0 | 4317, 4318 | OTLP 수신과 분배 |
| `prometheus`, `tempo`, `loki` | 3.15.1, 2.10.10, 3.7.9 | 9090 | 지표, 트레이스, 로그 |
| `grafana` | 13.3.0 | 3300 | 대시보드(관리자 비밀번호는 `.env`) |

모든 서비스 환경 변수: `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317`, `OTEL_SERVICE_NAME=<서비스 이름>`.

## 3. 채팅 DB(출발지 · 목적지 같은 스키마, `apps/demo/schema/`)

```sql
CREATE TABLE messages (
  id bigserial PRIMARY KEY,
  room text NOT NULL,
  author text NOT NULL,
  body text NOT NULL,
  idem_key uuid NOT NULL UNIQUE,      -- 클라이언트가 만든 멱등 키
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bravo_barrier (id bigserial PRIMARY KEY, plan_id text NOT NULL, epoch int NOT NULL, at timestamptz NOT NULL DEFAULT now());
CREATE TABLE bravo_fence (id int PRIMARY KEY CHECK (id = 1), epoch int NOT NULL);   -- 자원 쪽 펜스(D-19)
INSERT INTO bravo_fence VALUES (1, 0);
```

| 역할 | 권한 |
|---|---|
| `app_writer` | LOGIN. `messages` SELECT · INSERT, 시퀀스 USAGE |
| `replicator` | LOGIN REPLICATION. 두 표 SELECT |
| `executor_ctl` | LOGIN, CREATEROLE, `pg_signal_backend`. `bravo_*` 표 쓰기, `messages` SELECT, 시퀀스 setval 권한 |

- 출발지: `CREATE PUBLICATION bravo_fwd FOR TABLE messages, bravo_barrier`.
- 목적지: `CREATE SUBSCRIPTION bravo_fwd_sub CONNECTION 'host=src-db … user=replicator' PUBLICATION bravo_fwd`.
- 역방향: 목적지에 `bravo_rev` 발행, 출발지에 `bravo_rev_sub`를 `enabled=false, copy_data=false, origin=none, create_slot=false`로 미리 만들고 슬롯은 목적지에 미리 만듦.
- 게이트 1차 BLOCK 재료: 목적지에만 `GRANT SELECT ON messages TO PUBLIC`이 걸린 채로 시작합니다(심은 퇴행). `deploy/pilot/fix-dst-grant.sql`이 회수합니다.

## 4. PgBouncer

- `pool_mode=transaction`, DB 이름 `chat`, 처음 `host=src-db`.
- 설정 파일은 공유 볼륨의 `/etc/pgbouncer/pgbouncer.ini`. Executor만 `[databases]`를 바꾸고 관리 콘솔에 `RELOAD`를 보냅니다.
- 관리 사용자 `pgb_admin`(`admin_users`), 조회 사용자 `pgb_stats`(`stats_users`).

## 5. 플랫폼 DB(`platform/api/migrations/`가 주인, Executor는 아래 세 표에 씀)

```sql
CREATE TABLE runs (
  run_id uuid PRIMARY KEY, plan_id uuid NOT NULL, kind text NOT NULL,      -- cutover | failback
  epoch int NOT NULL, idem_key text NOT NULL UNIQUE, status text NOT NULL, -- RUNNING | DONE | ABORT_PRE_WRITE | SAFE_HOLD
  write_pause_ms int, started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz);
CREATE TABLE run_stages (
  run_id uuid NOT NULL REFERENCES runs, seq int NOT NULL, name text NOT NULL,
  started_at timestamptz NOT NULL, ms int, result text NOT NULL, detail jsonb, PRIMARY KEY (run_id, seq));
CREATE TABLE effects (
  effect_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs, resource text NOT NULL, action text NOT NULL,
  epoch int NOT NULL, state text NOT NULL,   -- PREPARED | IN_FLIGHT | CONFIRMED_APPLIED | CONFIRMED_NOT_APPLIED | UNKNOWN
  observed_at timestamptz, evidence jsonb, UNIQUE (run_id, resource, action));
CREATE TABLE receipts (
  receipt_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs, body jsonb NOT NULL,
  verdict text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
```

API가 더 두는 표: `tenants`, `principals(id, iss, sub, email)`, `memberships(principal_id, tenant_id, role)`, `environments`, `plans`, `events`, `approvals`, `idempotency`. 모든 업무 표에 `tenant_id`, RLS `FORCE`(파일럿은 테넌트 `t1` 하나).

## 6. Executor HTTP(`:8081`)

| 요청 | 응답 |
|---|---|
| `GET /v1/readiness` | `{lag_bytes, sub_state, direction, schema_hash:{src,dst}, seq:{src,dst}, rows:{src,dst}}`. `direction`은 지금 켜진 복제 방향(`forward` · `reverse` · `none`). `lag_bytes` · `sub_state`는 그 방향의 슬롯 · 구독 값이고, `none`이면 `null` · `"none"` |
| `GET /v1/controls` | `[{control_id, question, src, dst}]`. 값은 `PASS`, `FAIL`, `UNKNOWN`. 파일럿 통제: `DB-ANON-READ`(PUBLIC이 `messages`를 읽을 수 있는가 → 읽을 수 없으면 PASS), `DB-AUTH`(app 역할이 비밀번호 없이 접속 가능한가 → 불가면 PASS) |
| `POST /v1/cutover` `{plan_id, epoch, approval_id, write_pause_budget_ms}` + 헤더 `Idempotency-Key` | `202 {run_id}`. 같은 키 재요청은 같은 `run_id` |
| `POST /v1/failback` 본문 · 헤더는 cutover와 같음 | `202 {run_id}`. `runs.kind=failback`, 영수증 `direction=failback`. 같은 키 재요청은 같은 `run_id` |
| `GET /v1/runs/{run_id}` | `{status, write_pause_ms, stages[], receipt}`(더해서 `kind`, `effects[]`) |
| `GET /healthz` | `{ok, observe_only, observe_reason}`. `observe_only=true`면 새 실행은 `503 OBSERVE_ONLY` |

컷오버 단계(§8.6, `docs/architecture/03-migration.md` 4.2): 1 지연 확인 · epoch(`bravo_fence`) → 2 · 3 PgBouncer `PAUSE` → 4 출발지 `app_writer` `NOLOGIN` + 세션 종료 → 5 barrier commit · 도착 확인 · 최종 비교(행 수, PK 집합 해시, 내용 해시, 시퀀스) · 시퀀스 올림 → 6a 정방향 구독 끔 · 역방향 구독 켬 → 6b 확정 기록 → 7 목적지 `app_writer` LOGIN, PgBouncer 대상 변경 · `RELOAD` · `RESUME` → 8 PgBouncer로 쓰기 확인. 6a 전 실패는 ABORT_PRE_WRITE(원복), 뒤는 SAFE_HOLD. 바깥 변경마다 `effects` 행을 먼저 씁니다.

추가(2026-11-02, Executor): 4단계는 `NOLOGIN`과 함께 `messages` INSERT를 REVOKE하고(ABORT 때 GRANT로 원복), 6b는 확정 기록 뒤 정방향 슬롯을 지웁니다. 실행은 첫 효과 전에 `resource_locks`(Executor 마이그레이션 `platform/executor/migrations/`)에 자원 집합을 한 번에 예약합니다.

되돌리기 단계(FR-11, `03-migration.md` 4.3, 단계 이름 접두어 `failback.`): 1 역방향 지연 · epoch → 2 PAUSE → 2 목적지 `app_writer` `NOLOGIN` · REVOKE · 세션 종료 → 3·4 목적지 barrier · 출발지 도착 · 최종 비교 · 출발지 시퀀스 올림 → 6 역방향 구독 끔 → 6 확정 기록 · 역방향 슬롯 삭제 → 6 출발지 GRANT · LOGIN, PgBouncer 대상 출발지 · `RELOAD` · `RESUME` → 7 PgBouncer로 쓰기 확인. 역방향 구독을 끄기 전 실패는 ABORT_PRE_WRITE(목적지 writer 복원, `RESUME`), 뒤는 SAFE_HOLD.

추가(2026-11-02 19:50, Executor, D-04 첨부):
- 쓰기 정지 바로 앞 단계 `1b_freeze_attachments`: 출발지(와 아직 안 얼린 목적지)의 `attachment_store`를 동결합니다. 한 트랜잭션에서 `LOCK TABLE attachments IN SHARE ROW EXCLUSIVE MODE`(진행 중 INSERT를 기다림, `lock_timeout` 2초) 뒤 `frozen = true, freeze_watermark = clock_timestamp()`. 8단계(되돌리기는 7)에서 확인 쓰기 뒤 목적지 `active_store`를 바꾸고 동결을 푸는 일을 한 트랜잭션으로 합니다(`generation + 1`). ABORT_PRE_WRITE는 이 실행이 얼린 쪽을 원래 저장소 · 원래 동결 상태로 되돌립니다. 셋 다 효과 행(`<side>/db/attachment_store` `FREEZE`, `SWITCH_AND_UNFREEZE`, `RESTORE`)입니다.
- 영수증 해시 규칙 `v1.1`: v1에 uuid PK를 더함. uuid PK 표는 `ORDER BY pk::text COLLATE "C"`, PK 줄은 표준 소문자 uuid 문자열. 정수 PK 표의 해시는 v1과 같습니다. 비교 표: `messages`, `bravo_barrier`, `attachments`, `message_attachments`.
- 영수증 `objects`: 객체 저장소 두 곳(`BRAVO_ATT_SRC_S3_*`, `BRAVO_ATT_DST_S3_*`)이 설정되면 동결 뒤 남은 키를 복사하고 대상에서 SHA-256을 확인해 `MATCH`/`MISMATCH`(MISMATCH면 영수증 MISMATCH). 설정이 없으면 `NOT_CHECKED`와 참조 키 수만 적고 판정에 넣지 않습니다.
- 복사 작업: `python -m bravo_executor.attachment_copy --from src --to dst`. 기록은 플랫폼 DB `attachment_copies`(Executor 마이그레이션 0002).

## 7. API(`:8080`)와 운영자 MCP(`:8082/mcp`)

- REST: `platform/api/README.md`의 §8.9 표를 따릅니다. 파일럿에서 만드는 것: `POST /v1/environments`, `POST /v1/plans`, `GET /v1/plans/{id}`, `POST /v1/plans/{id}/gate`, `POST /v1/plans/{id}/approvals`(콘솔 전용, 승인자는 세션에서), `POST /v1/plans/{id}/shift`(`{"stage":"cutover"}`, 콘솔 전용), `GET /v1/plans/{id}/events`(SSE, `Last-Event-ID`).
- 쓰기 요청은 `Idempotency-Key` 필수. 같은 키에 다른 입력이면 409.
- 상태(스펙 §8.2 이름): `COLLECT(실제 수집) → ANALYZE → GATE | PROPOSE → REHEARSE → GATE → AWAIT_APPROVAL → CUTOVER → WATCH → DONE | FAILBACK → DONE_ROLLED_BACK`, 실패 `ABORT_PRE_WRITE → AWAIT_APPROVAL`, `SAFE_HOLD`, `NEEDS_HUMAN`. 계획의 `rehearsal` 값은 `not_started`, `running`, `passed`, `failed`, `infra_error`, `needs_human`입니다(옛 `not_in_scope`는 없어짐, 마이그레이션 0003). 이벤트 `mode`는 `live`, `recorded`뿐입니다.
- 게이트: Executor의 `/v1/controls`와 `/v1/readiness`를 읽어 결정적 규칙으로 판정(`docs/architecture/04-governance.md` 2절 표). 하나라도 퇴행 · UNKNOWN이면 BLOCK → 상태는 GATE에 남음.
- SSE 이벤트: `{id, plan_id, state, kind, at, mode, run_id, actor:{kind, principal}, data}`. `kind`: `state.changed`, `gate.verdict`, `approval.granted`, `run.stage`, `run.done`, `receipt.saved`, `analysis.done`, `trial.started`, `trial.proposed`, `tool.denied`, `trial.verdict`.
- 운영자 MCP 도구는 일곱 개(`docs/architecture/08-operator-mcp.md`): `env_register`, `env_controls`, `plan_create`, `plan_get`, `plan_events`, `gate_run`, `rehearsal_start`. 컷오버 · 되돌리기 · 승인 도구는 없습니다. 결과는 서버가 만든 상태 · ID · 이유 코드만 담습니다.
- 인증: `Authorization: Bearer <JWT>`(MCP) 또는 `Cf-Access-Jwt-Assertion`(콘솔 경유). 검증 규칙은 `08` 5절. 파일럿 공개 키는 `deploy/pilot/devauth/jwks.json`, 발급자 `https://bravo-dev.cloudflareaccess.com`, AUD는 `console-aud`, `mcp-aud`. 사용자 셋: `operator@bravo.test`(operator), `approver@bravo.test`(approver), `viewer@bravo.test`(viewer). 토큰은 `deploy/pilot/devauth/tokens/*.jwt`.

## 8. 콘솔(`:5173`)

한 화면(FR-13): 상태 머신 진행 막대, 게이트 판정표(통제 ID별 원본 · 대상 · 판정), 승인 버튼(승인자 토큰), 컷오버 시작 버튼, 단계별 ms와 쓰기 정지 ms, 무결성 영수증, 이벤트 흐름(`live` · `recorded` 배지, `actor.kind`). API 주소는 컨테이너 시작 때 환경 변수 `BRAVO_API_BASE`로 만든 `config.json`에서 읽습니다.
