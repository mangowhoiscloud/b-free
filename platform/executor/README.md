# platform/executor

Cutover Executor, 무결성 영수증, 되돌리기, Exit Drill입니다. 작업은 T12, T13, T20, T27입니다. 구현 언어는 담당이 11/3 착수 전에 정합니다(§12).

## 계약(§8.6). 언어와 무관하게 고정

| 항목 | 계약 |
|---|---|
| 호출 | 상태 머신이 HTTP로 호출, JSON, 모든 실행 요청에 `Idempotency-Key` |
| 입력 | `plan_id`, `run_id`, `epoch`, 출발지 · 목적지 접속 정보 참조, 쓰기 정지 예산, deadline |
| 출력 | 단계별 ms, 쓰기 정지 ms, 무결성 영수증, 결과(`DONE`, `ABORT_PRE_WRITE`, `SAFE_HOLD`). 제안 D-09는 `FAILBACK_REQUIRED`를 더함 |
| 안전 규칙 | 같은 키는 한 번만 실행. 응답을 잃으면 같은 호출을 반복하지 않고 실제 상태를 다시 읽음. 낮은 epoch 거부. 바깥 변경은 효과 원장에 먼저 기록(제안 D-19) |
| 배치(제안 D-19) | StatefulSet, replicas 1. 같은 Pod 신원이 둘 이상 뜨지 않게 하기 위해서임. 런북은 이 Pod의 강제 삭제(`--force --grace-period=0`)를 금지함 |
| 고객 클러스터 K8s 권한 | PgBouncer Pod 이름 하나에 묶은 `pods/exec`만(RBAC `resourceNames`). ConfigMap 쓰기 권한 없음. 대상 전환 방식은 `docs/architecture/03-migration.md` 4.1절 |
| 바깥 송신 | 플랫폼 DB, kube-dns, `bravo-link`의 링크만. AWS API 송신은 없음(아래 "바깥 쪽 펜싱" 마지막 줄) |
| 세션 생존 | 컷오버 준비부터 끝날 때까지 관리 · 제어 세션에 5초마다 heartbeat(PgBouncer는 `SHOW VERSION`, DB는 `SELECT 1`). DB 클라이언트는 `keepalives`와 `tcp_user_timeout`. 근거는 `docs/architecture/02-network.md` 4.4절 |

재조회 항목, 컷오버 단계, ABORT 절차, 되돌리기 단계는 `docs/architecture/03-migration.md` 4절에 있습니다.

## 효과 원장(제안 D-19)

상태 머신의 업무 상태와 별도로, Executor가 바깥에 내는 변경 하나하나를 플랫폼 DB의 `effects` 행으로 남깁니다. 응답을 잃었을 때 무엇을 다시 읽을지를 Executor의 기억에 맡기지 않기 위해서입니다. 출처는 2026-11-02 팀원 메모("설계 추가 제안" 2절)입니다.

**P0 범위.** `effects` 표, `resource_locks` 표, 고객 DB의 `bravo_fence`, 시험 E1–E6입니다. P0에는 자동 인계가 없습니다. 미해결 효과가 남은 채 Executor가 다시 뜨면 관찰 전용 모드로 뜨고, 사람이 런북으로 해소합니다. 더 강한 증거를 갖춘 자동 인계는 P1입니다.

**효과 행 하나 = 독립된 바깥 호출 하나.** 두 PgBouncer에 같은 명령을 보내면 행이 둘입니다. 보상 효과(ABORT 복원, 동결 해제 등)는 `compensates`로 원효과를 가리킵니다.

| 단계 | 효과(행마다 하나) | 실제 상태를 읽는 곳 |
|---|---|---|
| SHIFT 시작 | (D-04) 첨부 동결 | 쓰기 DB의 `attachment_store` |
| SHIFT | Edge 가중치 단계 하나 | Cloudflare LB pool API |
| SHIFT 실패 | 가중치 0, (D-04) 첨부 동결 해제 | Cloudflare LB pool API, `attachment_store` |

SHIFT 두 행(가중치 단계, 0% 복귀)은 `src/bravo_executor/front.py`, `front_api.py`입니다(계약 15 §10). 단계 판정은 대표 이름으로 보낸 실제 채팅 쓰기(`POST /api/rooms/shift-probe/messages`, 초당 60건)에서 목적지가 답한 쓰기 100건 이상, 오류율 1% 이하, p95 800 ms 이하입니다. 보유는 5초 이상 30초 이하입니다. 계획의 SHIFT는 컷오버 승인을 확인하고(콘솔 시작 = 소비된 승인), 컷오버는 같은 승인의 shift 실행이 DONE이어야 시작합니다(`SHIFT_NOT_DONE`). 되돌리기는 마지막 단계(`failback.8_front_to_src`)에서 앞단을 출발지 100%로 옮깁니다. 실측은 `docs/evidence/shift/`.
| 2 | 진입점 쓰기 차단 | 차단 플래그 |
| 3 | PgBouncer PAUSE(출발지, 목적지 각 한 행) | `SHOW STATE` |
| 4 | 출발지 writer 제한(`NOLOGIN`, REVOKE) | DB 카탈로그 |
| 5 | barrier commit | 목적지 DB의 barrier 행 |
| 5 | 목적지 시퀀스 올림 | 목적지 `pg_sequences` |
| 6a | 정방향 apply 정지, 역방향 구독 켬 | `pg_subscription`, `pg_stat_subscription` |
| 6b | 정방향 슬롯 삭제(되돌릴 수 없음) | `pg_replication_slots` |
| 7 | 목적지 writer 켬 | DB 카탈로그 |
| 1 전(타이머 밖) | 효과 아님. `pods/exec`로 두 PgBouncer Pod 안에 `target-src.ini`, `target-dst.ini`가 있는지 확인. 없으면 시작하지 않음 | Pod 안 파일 |
| 7 | PgBouncer 대상 변경: `pods/exec`로 emptyDir의 활성 파일을 원자적으로 교체, RELOAD, RESUME(출발지, 목적지 각 한 행) | `SHOW DATABASES`의 host, `SHOW STATE`. host가 새 대상이 아니면 `UNKNOWN`, SAFE_HOLD |
| 8 | 진입점 쓰기 차단 해제 | 차단 플래그 |
| 8 | (D-04) `active_store`를 목적지로 바꾸고 첨부 동결 해제(한 트랜잭션) | 목적지 DB의 `attachment_store` |
| ABORT_PRE_WRITE | 복원 하나하나(역방향 구독 끔, 정방향 apply 켬, writer 복원, PgBouncer RESUME, 진입점 해제, 첨부 동결 해제). 이 실행이 만든 제한만 복원 | 원효과와 같음 |
| FAILBACK | `03-migration.md` 4.3절의 단계마다 같은 규칙 | 원효과와 같음 |

**행의 필드.** 접속 비밀은 값 대신 등록된 참조 이름으로 적습니다.

| 묶음 | 필드 |
|---|---|
| 범위 | `tenant_id`, `plan_id`, 정규화한 자원 ID(예: `dst/pgbouncer/0`, `src/db/role:app_writer`) |
| 의도 | 계획 digest, 단계, 동작, 방향(`cutover`, `failback`, `abort`), 입력 digest |
| 실행권 | `run_id`, owner, `epoch`, lease 만료 |
| 효과 키 | (`run_id`, 단계, 자원, 동작, 방향, 시도). UNIQUE |
| 연결 | `compensates`(보상 효과가 가리키는 원효과) |
| 결과 | 상태, 관측 시각, 근거 참조 |
| 해소 | 미해결 사유, 해소한 사람, 최종 판정 |

**상태.** `PREPARED`(의도 저장, 호출 전) → `IN_FLIGHT`(호출 시작) → `CONFIRMED_APPLIED` 또는 `CONFIRMED_NOT_APPLIED` 또는 `UNKNOWN`. 시간 초과나 "지금 값이 원래 값과 같다"만으로는 `CONFIRMED_NOT_APPLIED`를 주지 않습니다. 옛 요청이 나중에 적용될 수 있기 때문입니다. `CONFIRMED_NOT_APPLIED`에는 "적용되지 않음"의 관측과 "그 요청이 더는 적용될 수 없음"의 증거가 둘 다 필요합니다. 증거의 종류는 아래 "바깥 쪽 펜싱"입니다.

**PAUSE 보냄, 응답 없음.** 3단계에서 PAUSE를 보낸 뒤 응답 전에 링크가 끊기면 그 행은 `UNKNOWN`입니다. 링크는 프록시라서, 이미 넘긴 명령이 원격에서 늦게 처리될 수 있습니다.
- 6a 전이라도 ABORT_PRE_WRITE는 그 행이 `CONFIRMED_NOT_APPLIED`일 때만 합니다. 즉 `SHOW STATE`가 paused가 아니고, 아래 PgBouncer 행의 해소 조건(옛 관리 연결 없음)을 채워야 합니다.
- 증명하지 못하면 SAFE_HOLD입니다. 시험은 `docs/architecture/02-network.md` 13절의 N13b입니다.

**자원 잠금.** 서로 다른 효과 행이나 다른 계획은 각자의 CAS에 성공할 수 있습니다. 그래서 실행 단위로 자원을 먼저 잡습니다.
- 실행이 건드릴 자원 ID를 정규화합니다. 같은 자원은 언제나 같은 ID입니다.
- 첫 `PREPARED` 전에 충돌하는 자원 집합 전체를 `resource_locks`에 한 트랜잭션으로 예약합니다. 하나라도 다른 실행이 잡고 있으면 아무것도 잡지 않고 거부합니다.
- 잠금은 그 실행이 끝나고 모든 효과가 `CONFIRMED_*`가 될 때까지 둡니다. lease 만료만으로 풀리지 않습니다.

**실행 순서.**
1. 자원 집합을 `resource_locks`에 예약합니다.
2. `PREPARED`를 commit합니다.
3. 같은 행을 owner, epoch, lease를 조건으로 `IN_FLIGHT`로 갱신(CAS)하고 commit합니다. 이 트랜잭션은 보안 보류의 guard 행을 `FOR UPDATE`로 잠그고 보류를 확인합니다(D-20). 이 commit이 효과의 발행 시점입니다.
4. 행 잠금 없이 바깥 호출을 보냅니다. DB 효과는 아래 `bravo_fence` 갱신으로 시작하는 트랜잭션 안에서 보냅니다.
5. 응답과 실제 자원 상태를 관측해 결과를 적습니다. 결과 갱신은 owner, epoch, 현재 상태(`IN_FLIGHT`)를 조건으로 합니다.
6. 응답을 잃으면 `UNKNOWN`으로 두고 다시 호출하지 않습니다.

**재시작과 관찰 전용 모드.** P0에는 자동 인계가 없습니다.
- 미해결 효과(`PREPARED`, `IN_FLIGHT`, `UNKNOWN`)가 남은 채 Executor가 다시 뜨면 관찰 전용 모드로 뜹니다. 진행 중이던 실행을 이어 가지 않습니다.
- 관찰 전용 모드는 읽기 전용 자격만 받습니다. DB 역할은 `executor_observe`(카탈로그와 상태 조회만)이고, PgBouncer는 `stats_users`(SHOW 명령만)입니다. 쓰기 자격이 없으므로 모드 플래그가 틀려도 새 변경을 내지 못합니다.
- 계획은 SAFE_HOLD로 갑니다. 사람이 런북 `docs/runbooks/safe-hold.md`(작성 예정)를 따라 효과마다 실제 상태와 근거를 확인하고 판정을 적습니다.
- 모든 효과가 `CONFIRMED_*`가 되면 사람이 SAFE_HOLD에서 다음 동작을 정합니다. 승인이 취소된 뒤 옛 호출의 응답이 오면 관측은 기록하지만, 다음 단계는 새 검사를 거쳐야 합니다.

**바깥 쪽 펜싱.** epoch는 플랫폼 DB 안의 숫자입니다. 바깥 자원이 옛 Executor의 늦은 명령을 거부하려면 자원 쪽에 펜스가 있어야 합니다. 자원 종류마다 다릅니다.

| 자원 | 자원 쪽 펜스 | 효과를 해소하는 조건 |
|---|---|---|
| 출발지 · 목적지 DB | `bravo_fence(epoch)` 행 하나. 양쪽 DB에 따로 두고 발행(publication)에서 뺌 | 펜스가 옛 epoch의 명령을 거부하므로, 관측한 상태로 판정 |
| PgBouncer 관리 콘솔 | 없음 | 각 PgBouncer의 `SHOW CLIENTS`에 옛 Executor의 관리 연결이 없을 때만. 고정한 판이 지원하면 `KILL_CLIENT`로 끊음(확인 필요). 아니면 `UNKNOWN`, 계획은 SAFE_HOLD |
| Edge API(Cloudflare) | 없음. 보낸 호출을 되돌릴 수 없음 | 사람이 확인하기 전까지 `UNKNOWN` |

- **DB 펜스.** Executor의 모든 제어 트랜잭션은 먼저 `UPDATE bravo_fence SET epoch = $e WHERE epoch <= $e`를 실행합니다. 갱신된 행이 0이면 그 트랜잭션을 버립니다. 새 실행이 높은 epoch를 쓰고 나면, 옛 Executor의 늦은 DB 명령은 자원에서 실패합니다.
- 트랜잭션 안에서 돌 수 없는 명령(구독 켜고 끄기, 슬롯 삭제 등)이 있으면, 그 효과는 자원 쪽 펜스가 없는 것으로 다룹니다(확인 필요).
- 관리 연결을 끊는 `KILL_CLIENT`는 관리 권한이 필요합니다. 관찰 전용 모드는 그 권한이 없으므로 사람이 런북으로 합니다.
- 진입점 차단 플래그는 저장 위치에 따라 위 셋 중 하나의 규칙을 따릅니다.
- `pg_terminate_backend(pid, timeout)`(timeout > 0)와 `pg_stat_activity` 재확인은 보조 수단입니다. 이것만으로 효과를 해소하지 않습니다.
- P0 Executor에는 AWS 자격이 없습니다. EC2 `StopInstances` 권한을 주지 않습니다. 노드를 멈춰야 하면 사람이 런북에 따라 직접 멈춥니다.
- 그래서 Executor의 AWS API 송신도 없습니다. 첨부 동결은 DB 트랜잭션(D-04)이고 S3 버킷 정책을 쓰지 않습니다.

**계약 시험(T12).** 모두 P0 완료 조건입니다.

| ID | 시험 | 통과 기준 |
|---|---|---|
| E1 | 효과 적용 직후 Executor를 죽임(응답 유실) | 다시 뜬 Executor가 같은 변경을 반복하지 않음. 관찰 전용 모드, 계획 SAFE_HOLD |
| E2 | 다른 실행이 같은 자원을 건드리는 실행을 시작 | `resource_locks` 예약 실패로 거부 |
| E3 | lease 만료만 일어남 | `UNKNOWN` 유지, 자원 잠금 유지 |
| E4 | 관찰 전용 모드에서 DB와 PgBouncer에 쓰기 시도 | 자격 부족으로 거부(모드 플래그와 무관) |
| E5 | 옛 epoch의 DB 명령을 새 epoch 기록 뒤에 늦게 도착시킴(지연 명령) | `bravo_fence` 갱신 0행으로 그 트랜잭션이 실패 |
| E6 | PgBouncer 관리 효과를 `IN_FLIGHT`로 둔 채 재시작, `SHOW CLIENTS`에 옛 연결이 남음 | `UNKNOWN`, 계획 SAFE_HOLD |

## 무결성 영수증(§8.6)

```text
receipt_id      epoch=   direction=cutover|failback
source          target
barrier_id      committed_at=
watermark       source_lsn=   received_lsn=
hash_rule       v1 (PK 정렬, NULL 처리, 타입 직렬화 고정)
tables[]        name  rows(src/dst)  pk_hash(src/dst)  content_hash(src/dst)
sequences[]     name  src_last  dst_set (올리기만)
objects[]       (D-04) key  sha256(src/dst)   대상은 DB가 참조하는 키. 대조 시각과 동결 watermark
write_pause_ms  stages=entry, pause, fence, compare, reconnect
verdict         MATCH | MISMATCH
```

영수증은 목적지 쓰기를 열기 전에 플랫폼 DB에 저장합니다(S3-AC2). 이 폴더는 LLM 클라이언트를 import하지 않습니다(NFR-5).
